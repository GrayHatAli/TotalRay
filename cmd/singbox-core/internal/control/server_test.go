package control

import (
	"bufio"
	"encoding/json"
	"errors"
	"net"
	"path/filepath"
	"sync"
	"testing"
)

// fakeController is a Controller test double that records every call it
// receives, so tests can assert the server dispatched the right method with
// the right arguments -- without pulling in a real, heavyweight sing-box
// Core.
type fakeController struct {
	mu sync.Mutex

	running     bool
	startCalls  [][]byte
	stopCalls   int
	addCalls    [][]byte
	removeCalls []string
	startErr    error
	stopErr     error
	addErr      error
	removeErr   error
}

func (f *fakeController) Start(config []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.startCalls = append(f.startCalls, append([]byte(nil), config...))
	if f.startErr != nil {
		return f.startErr
	}
	f.running = true
	return nil
}

func (f *fakeController) Stop() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stopCalls++
	if f.stopErr != nil {
		return f.stopErr
	}
	f.running = false
	return nil
}

func (f *fakeController) IsRunning() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.running
}

func (f *fakeController) AddOutbound(config []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.addCalls = append(f.addCalls, append([]byte(nil), config...))
	return f.addErr
}

func (f *fakeController) RemoveOutbound(tag string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removeCalls = append(f.removeCalls, tag)
	return f.removeErr
}

// testServer starts a Server on a temp-dir socket and returns it plus a
// ready-to-use client connection, cleaning both up on test end.
func testServer(t *testing.T, fc *fakeController) (*Server, net.Conn) {
	t.Helper()
	socketPath := filepath.Join(t.TempDir(), "singbox-core.sock")
	srv := &Server{SocketPath: socketPath, Controller: fc}
	if err := srv.Listen(); err != nil {
		t.Fatalf("Listen: %v", err)
	}
	go srv.Serve()
	t.Cleanup(func() { srv.Close() })

	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return srv, conn
}

func roundTrip(t *testing.T, conn net.Conn, req Request) Response {
	t.Helper()
	b, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	if _, err := conn.Write(append(b, '\n')); err != nil {
		t.Fatalf("write request: %v", err)
	}
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	var resp Response
	if err := json.Unmarshal(line, &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	return resp
}

func TestServerStartStopIsRunning(t *testing.T) {
	fc := &fakeController{}
	_, conn := testServer(t, fc)

	if resp := roundTrip(t, conn, Request{Cmd: "is_running"}); !resp.OK || resp.Running {
		t.Fatalf("initial is_running: %+v", resp)
	}

	startResp := roundTrip(t, conn, Request{Cmd: "start", Config: json.RawMessage(`{"outbounds":[]}`)})
	if !startResp.OK {
		t.Fatalf("start: %+v", startResp)
	}
	if len(fc.startCalls) != 1 {
		t.Fatalf("expected 1 Start call, got %d", len(fc.startCalls))
	}

	if resp := roundTrip(t, conn, Request{Cmd: "is_running"}); !resp.OK || !resp.Running {
		t.Fatalf("is_running after start: %+v", resp)
	}

	stopResp := roundTrip(t, conn, Request{Cmd: "stop"})
	if !stopResp.OK || fc.stopCalls != 1 {
		t.Fatalf("stop: resp=%+v stopCalls=%d", stopResp, fc.stopCalls)
	}
}

func TestServerAddRemoveOutbound(t *testing.T) {
	fc := &fakeController{}
	_, conn := testServer(t, fc)

	addResp := roundTrip(t, conn, Request{Cmd: "add_outbound", Config: json.RawMessage(`{"type":"direct","tag":"x"}`)})
	if !addResp.OK {
		t.Fatalf("add_outbound: %+v", addResp)
	}
	if len(fc.addCalls) != 1 {
		t.Fatalf("expected 1 AddOutbound call, got %d", len(fc.addCalls))
	}

	removeResp := roundTrip(t, conn, Request{Cmd: "remove_outbound", Tag: "x"})
	if !removeResp.OK {
		t.Fatalf("remove_outbound: %+v", removeResp)
	}
	if len(fc.removeCalls) != 1 || fc.removeCalls[0] != "x" {
		t.Fatalf("expected RemoveOutbound(\"x\"), got %v", fc.removeCalls)
	}
}

func TestServerRejectsMissingFields(t *testing.T) {
	fc := &fakeController{}
	_, conn := testServer(t, fc)

	if resp := roundTrip(t, conn, Request{Cmd: "start"}); resp.OK {
		t.Fatal("expected failure for start with no config")
	}
	if resp := roundTrip(t, conn, Request{Cmd: "add_outbound"}); resp.OK {
		t.Fatal("expected failure for add_outbound with no config")
	}
	if resp := roundTrip(t, conn, Request{Cmd: "remove_outbound"}); resp.OK {
		t.Fatal("expected failure for remove_outbound with no tag")
	}
	if resp := roundTrip(t, conn, Request{Cmd: "bogus"}); resp.OK {
		t.Fatal("expected failure for unknown cmd")
	}
}

func TestServerPropagatesControllerError(t *testing.T) {
	fc := &fakeController{startErr: errors.New("boom")}
	_, conn := testServer(t, fc)

	resp := roundTrip(t, conn, Request{Cmd: "start", Config: json.RawMessage(`{}`)})
	if resp.OK || resp.Error != "boom" {
		t.Fatalf("expected propagated error \"boom\", got %+v", resp)
	}
}

func TestServerMultipleRequestsOneConnection(t *testing.T) {
	fc := &fakeController{}
	_, conn := testServer(t, fc)

	for i := 0; i < 5; i++ {
		if resp := roundTrip(t, conn, Request{Cmd: "is_running"}); !resp.OK {
			t.Fatalf("request %d: %+v", i, resp)
		}
	}
}

func TestListenRemovesStaleSocket(t *testing.T) {
	socketPath := filepath.Join(t.TempDir(), "singbox-core.sock")

	first := &Server{SocketPath: socketPath, Controller: &fakeController{}}
	if err := first.Listen(); err != nil {
		t.Fatalf("first Listen: %v", err)
	}
	// Simulate an unclean shutdown: close the raw listener directly
	// (bypassing Server.Close, which would unlink the file itself) so
	// the socket file is left behind with nothing listening on it --
	// exactly what a crashed process leaves behind.
	if err := first.listener.Close(); err != nil {
		t.Fatalf("close raw listener: %v", err)
	}

	second := &Server{SocketPath: socketPath, Controller: &fakeController{}}
	if err := second.Listen(); err != nil {
		t.Fatalf("second Listen (stale socket cleanup): %v", err)
	}
	t.Cleanup(func() { second.Close() })
}
