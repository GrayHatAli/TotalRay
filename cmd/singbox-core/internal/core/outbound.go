package core

import (
	"fmt"

	"github.com/sagernet/sing-box/adapter"
	"github.com/sagernet/sing-box/option"
)

// AddOutbound registers a new outbound on the live Box from a single
// sing-box outbound JSON object (the same shape as one entry of a config's
// "outbounds" array), without restarting anything. It is a thin wrapper
// around adapter.OutboundManager.Create, following the same call shape as
// alireza0/s-ui's core.AddOutbound (core/endpoint.go).
//
// Returns ErrNotRunning if no Box is running.
func (c *Core) AddOutbound(configJSON []byte) error {
	b, err := c.running()
	if err != nil {
		return err
	}

	var out option.Outbound
	if err := out.UnmarshalJSONContext(c.ctx, configJSON); err != nil {
		return fmt.Errorf("unmarshal outbound config: %w", err)
	}
	if out.Tag == "" {
		return fmt.Errorf("outbound config missing tag")
	}

	// Matches s-ui: the outbound's own tag is threaded through the
	// context so a "final"/self-referencing chain outbound (if any)
	// resolves correctly during Create.
	outboundCtx := adapter.WithContext(c.ctx, &adapter.InboundContext{
		Outbound: out.Tag,
	})

	return b.Outbound().Create(
		outboundCtx,
		b.Router(),
		b.LogFactory().NewLogger("outbound/"+out.Type+"["+out.Tag+"]"),
		out.Tag,
		out.Type,
		out.Options,
	)
}

// RemoveOutbound removes the outbound with the given tag from the live Box,
// without restarting anything.
//
// Returns ErrNotRunning if no Box is running. Removing the tag that is
// currently selected by a selector/urltest group, or that is still carrying
// live connections, is the caller's responsibility to avoid -- Core applies
// no such guard itself, matching sing-box's own OutboundManager.Remove.
func (c *Core) RemoveOutbound(tag string) error {
	b, err := c.running()
	if err != nil {
		return err
	}
	if tag == "" {
		return fmt.Errorf("tag must not be empty")
	}
	return b.Outbound().Remove(tag)
}
