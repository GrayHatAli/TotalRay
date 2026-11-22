package core

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sync/atomic"
	"testing"
	"time"

	"github.com/miekg/dns"
	"github.com/sagernet/sing-box/adapter"
	"github.com/sagernet/sing/service"
)

// startFakeDNS serves DNS over TCP on loopback and answers every A query with
// a fixed address. It returns the port it listens on.
func startFakeDNS(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen fake dns: %v", err)
	}
	srv := &dns.Server{
		Listener: ln,
		Handler: dns.HandlerFunc(func(w dns.ResponseWriter, r *dns.Msg) {
			m := new(dns.Msg)
			m.SetReply(r)
			if len(r.Question) > 0 && r.Question[0].Qtype == dns.TypeA {
				m.Answer = append(m.Answer, &dns.A{
					Hdr: dns.RR_Header{Name: r.Question[0].Name, Rrtype: dns.TypeA,
						Class: dns.ClassINET, Ttl: 1},
					A: net.ParseIP("10.9.8.7").To4(),
				})
			}
			_ = w.WriteMsg(m)
		}),
	}
	go func() { _ = srv.ActivateAndServe() }()
	t.Cleanup(func() { _ = srv.Shutdown() })
	return ln.Addr().(*net.TCPAddr).Port
}

// fakeSocks is a minimal SOCKS5 CONNECT proxy that counts the tunnels it has
// opened. It stands in for a real proxy leaf so a test can tell which leaf a
// DNS query travelled through, and can be "killed" like a dead server.
type fakeSocks struct {
	ln    net.Listener
	conns atomic.Int32
}

func startFakeSocks(t *testing.T) *fakeSocks {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen fake socks: %v", err)
	}
	s := &fakeSocks{ln: ln}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go s.handle(conn)
		}
	}()
	return s
}

func (s *fakeSocks) port() int { return s.ln.Addr().(*net.TCPAddr).Port }

func (s *fakeSocks) handle(c net.Conn) {
	defer c.Close()
	buf := make([]byte, 300)
	if _, err := io.ReadFull(c, buf[:2]); err != nil { // VER NMETHODS
		return
	}
	if _, err := io.ReadFull(c, buf[:int(buf[1])]); err != nil { // METHODS
		return
	}
	if _, err := c.Write([]byte{5, 0}); err != nil {
		return
	}
	if _, err := io.ReadFull(c, buf[:4]); err != nil { // VER CMD RSV ATYP
		return
	}
	atyp := buf[3]
	var host string
	switch atyp {
	case 1:
		if _, err := io.ReadFull(c, buf[:4]); err != nil {
			return
		}
		host = net.IP(append([]byte(nil), buf[:4]...)).String()
	case 3:
		if _, err := io.ReadFull(c, buf[:1]); err != nil {
			return
		}
		n := int(buf[0])
		if _, err := io.ReadFull(c, buf[:n]); err != nil {
			return
		}
		host = string(buf[:n])
	default:
		return
	}
	if _, err := io.ReadFull(c, buf[:2]); err != nil {
		return
	}
	port := int(buf[0])<<8 | int(buf[1])
	dst, err := net.Dial("tcp", net.JoinHostPort(host, fmt.Sprint(port)))
	if err != nil {
		_, _ = c.Write([]byte{5, 5, 0, 1, 0, 0, 0, 0, 0, 0})
		return
	}
	defer dst.Close()
	if _, err := c.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0}); err != nil {
		return
	}
	s.conns.Add(1)
	go func() { _, _ = io.Copy(dst, c) }()
	_, _ = io.Copy(c, dst)
}

func lookupVia(c *Core, name string) ([]netip.Addr, error) {
	router := service.FromContext[adapter.DNSRouter](c.ctx)
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	return router.Lookup(ctx, name, adapter.DNSQueryOptions{DisableCache: true})
}

// TestReloadDNSServerRebindsDetourAfterGroupReplacement reproduces the
// production failure: a DNS server whose detour is the "select" group keeps
// using the ORIGINAL select -> auto -> leaf chain after the groups are
// replaced at runtime (what StandbyPool.reconcile does), so it dies with that
// leaf. Reloading the DNS server binds it to the current chain.
func TestReloadDNSServerRebindsDetourAfterGroupReplacement(t *testing.T) {
	dnsPort := startFakeDNS(t)
	oldProxy := startFakeSocks(t)
	newProxy := startFakeSocks(t)

	dnsServer := fmt.Sprintf(
		`{"type":"tcp","tag":"remote","server":"127.0.0.1","server_port":%d,"detour":"select"}`, dnsPort)
	cfg := fmt.Sprintf(`{
	  "log": {"disabled": true},
	  "dns": {"servers": [%s], "final": "remote"},
	  "inbounds": [{"type":"mixed","tag":"mixed-in","listen":"127.0.0.1","listen_port":0}],
	  "outbounds": [
	    {"type":"direct","tag":"direct"},
	    {"type":"socks","tag":"leaf-old","server":"127.0.0.1","server_port":%d,"version":"5"},
	    {"type":"selector","tag":"auto","outbounds":["leaf-old"],"default":"leaf-old"},
	    {"type":"selector","tag":"select","outbounds":["auto"],"default":"auto"}
	  ],
	  "route": {"final": "select"}
	}`, dnsServer, oldProxy.port())

	c := New()
	if err := c.Start([]byte(cfg)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	// Baseline: DNS resolves through the original chain.
	if _, err := lookupVia(c, "before.test"); err != nil {
		t.Fatalf("baseline lookup through the original chain failed: %v", err)
	}
	if oldProxy.conns.Load() == 0 {
		t.Fatal("baseline lookup did not travel through the original leaf")
	}

	// Replace the groups exactly like StandbyPool.reconcile: add the new
	// leaf, remove select then auto, re-add auto then select, retire the
	// old leaf.
	mustDo := func(what string, err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("%s: %v", what, err)
		}
	}
	mustDo("add leaf-new", c.AddOutbound([]byte(fmt.Sprintf(
		`{"type":"socks","tag":"leaf-new","server":"127.0.0.1","server_port":%d,"version":"5"}`, newProxy.port()))))
	mustDo("remove select", c.RemoveOutbound("select"))
	mustDo("remove auto", c.RemoveOutbound("auto"))
	mustDo("add auto", c.AddOutbound([]byte(
		`{"type":"selector","tag":"auto","outbounds":["leaf-new"],"default":"leaf-new"}`)))
	mustDo("add select", c.AddOutbound([]byte(
		`{"type":"selector","tag":"select","outbounds":["auto"],"default":"auto"}`)))
	mustDo("remove leaf-old", c.RemoveOutbound("leaf-old"))

	// The old leaf's server dies, as the production one did.
	_ = oldProxy.ln.Close()

	// Without the reload the DNS server is still bound to the retired
	// chain, so resolution fails even though the live groups are healthy.
	if _, err := lookupVia(c, "stale.test"); err == nil {
		t.Fatal("expected the un-reloaded DNS server to fail through the retired chain " +
			"(the stale-detour defect did not reproduce)")
	}

	// After the reload it follows the current chain.
	if err := c.ReloadDNSServer([]byte(dnsServer)); err != nil {
		t.Fatalf("ReloadDNSServer: %v", err)
	}
	addrs, err := lookupVia(c, "after.test")
	if err != nil {
		t.Fatalf("lookup after ReloadDNSServer failed: %v", err)
	}
	if len(addrs) == 0 || addrs[0].String() != "10.9.8.7" {
		t.Fatalf("unexpected answer after reload: %v", addrs)
	}
	if newProxy.conns.Load() == 0 {
		t.Fatal("lookup after reload did not travel through the new leaf")
	}
}

func TestReloadDNSServerRequiresRunning(t *testing.T) {
	c := New()
	err := c.ReloadDNSServer([]byte(`{"type":"udp","tag":"x","server":"127.0.0.1"}`))
	if err != ErrNotRunning {
		t.Fatalf("ReloadDNSServer before Start: got %v, want ErrNotRunning", err)
	}
}

func TestReloadDNSServerRejectsBadConfig(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if err := c.ReloadDNSServer([]byte(`{"type":"udp","server":"127.0.0.1"}`)); err == nil {
		t.Fatal("expected error for a DNS server config with no tag")
	}
	if err := c.ReloadDNSServer([]byte(`not json`)); err == nil {
		t.Fatal("expected error for malformed DNS server config")
	}
}
