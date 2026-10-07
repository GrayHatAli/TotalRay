package core

import "testing"

// hasOutbound is a small test helper reading the live Box's outbound
// manager directly -- exercising the exact same accessor AddOutbound and
// RemoveOutbound use, so a pass here means the daemon's own code path
// actually changed the running set.
func hasOutbound(t *testing.T, c *Core, tag string) bool {
	t.Helper()
	b, err := c.running()
	if err != nil {
		t.Fatalf("running: %v", err)
	}
	_, ok := b.Outbound().Outbound(tag)
	return ok
}

func TestAddOutboundNoRestart(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	instanceBefore := c.instance
	if hasOutbound(t, c, "extra") {
		t.Fatal("outbound \"extra\" already present before AddOutbound")
	}

	newOutbound := []byte(`{"type":"direct","tag":"extra"}`)
	if err := c.AddOutbound(newOutbound); err != nil {
		t.Fatalf("AddOutbound: %v", err)
	}

	if c.instance != instanceBefore {
		t.Fatal("AddOutbound replaced the running Box instance (should never restart)")
	}
	if !hasOutbound(t, c, "extra") {
		t.Fatal("outbound \"extra\" missing after AddOutbound")
	}
	// The original outbounds from the config must be untouched.
	if !hasOutbound(t, c, "direct") || !hasOutbound(t, c, "block") {
		t.Fatal("original outbounds disturbed by AddOutbound")
	}
}

func TestRemoveOutboundNoRestart(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if err := c.AddOutbound([]byte(`{"type":"direct","tag":"extra"}`)); err != nil {
		t.Fatalf("AddOutbound: %v", err)
	}

	instanceBefore := c.instance
	if err := c.RemoveOutbound("extra"); err != nil {
		t.Fatalf("RemoveOutbound: %v", err)
	}

	if c.instance != instanceBefore {
		t.Fatal("RemoveOutbound replaced the running Box instance (should never restart)")
	}
	if hasOutbound(t, c, "extra") {
		t.Fatal("outbound \"extra\" still present after RemoveOutbound")
	}
	// The tag not touched by RemoveOutbound must be undisturbed -- this is
	// the same property the manual test in the plan checks via live
	// connection counts: removing one tag must not affect any other.
	if !hasOutbound(t, c, "direct") {
		t.Fatal("unrelated outbound \"direct\" disturbed by RemoveOutbound")
	}
}

func TestAddOutboundRejectsMissingTag(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if err := c.AddOutbound([]byte(`{"type":"direct"}`)); err == nil {
		t.Fatal("expected error for outbound config with no tag")
	}
}

func TestRemoveOutboundUnknownTagFails(t *testing.T) {
	c := New()
	if err := c.Start(throwawayConfig(t)); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer c.Stop()

	if err := c.RemoveOutbound("does-not-exist"); err == nil {
		t.Fatal("expected error removing an unknown tag")
	}
}
