package core

import (
	"os"
	"path/filepath"
	"testing"
)

// throwawayConfig is a minimal, network-free sing-box config: a mixed
// inbound on an OS-assigned loopback port, plus "direct" and "block"
// outbounds. It never dials out, so tests run offline and don't touch
// production in any way.
func throwawayConfig(t *testing.T) []byte {
	t.Helper()
	path := filepath.Join("..", "..", "testdata", "config.json")
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read testdata config: %v", err)
	}
	return b
}

func TestStartStopIsRunning(t *testing.T) {
	c := New()
	if c.IsRunning() {
		t.Fatal("new Core reports running before Start")
	}

	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if !c.IsRunning() {
		t.Fatal("Core does not report running after Start")
	}

	if err := c.Stop(); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if c.IsRunning() {
		t.Fatal("Core still reports running after Stop")
	}

	// Stop on an already-stopped Core is a no-op, not an error.
	if err := c.Stop(); err != nil {
		t.Fatalf("second Stop: %v", err)
	}
}

func TestStartTwiceFails(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if err := c.Start(throwawayConfig(t)); err == nil {
		t.Fatal("expected second Start to fail while already running")
	}
}

func TestAddRemoveOutboundRequiresRunning(t *testing.T) {
	c := New()

	if err := c.AddOutbound([]byte(`{"type":"direct","tag":"x"}`)); err != ErrNotRunning {
		t.Fatalf("AddOutbound before Start: got %v, want ErrNotRunning", err)
	}
	if err := c.RemoveOutbound("x"); err != ErrNotRunning {
		t.Fatalf("RemoveOutbound before Start: got %v, want ErrNotRunning", err)
	}
}
