// Package core owns the lifecycle of a single embedded sing-box instance.
//
// This mirrors the design of alireza0/s-ui's core.Core (core/main.go,
// core/box.go, core/endpoint.go -- GPL-3.0, reuse permitted): a small
// wrapper that starts/stops a *box.Box and, once it is running, applies
// live outbound changes to it via adapter.OutboundManager.Create/Remove
// without ever restarting the process.
//
// Unlike s-ui, this package does not vendor sing-box's box.go -- the
// upstream github.com/sagernet/sing-box package already exports the
// Router(), Outbound(), and LogFactory() accessors this needs, so we
// depend on it directly instead of copying internals.
package core

import (
	"context"
	"errors"
	"fmt"
	"sync"

	box "github.com/sagernet/sing-box"
	"github.com/sagernet/sing-box/include"
	"github.com/sagernet/sing-box/option"
)

// ErrNotRunning is returned by any live-instance operation (AddOutbound,
// RemoveOutbound) when called while no Box is running.
var ErrNotRunning = errors.New("singbox-core: sing-box is not running")

// Core owns the running *box.Box, if any. Everything mutable lives behind
// mu; the Box is read off Core rather than cached elsewhere, so a caller can
// never mix a manager from one instance with a Box that has since been
// replaced by a Stop+Start cycle.
//
// mu is a RWMutex because IsRunning is expected to be polled far more often
// than Start/Stop are called, and must never queue behind a slow start.
//
// lifecycleMu serializes Start and Stop against each other (building and
// closing a Box are not cheap and must not race one another). It is the
// outer lock; mu is the inner one -- mirroring s-ui's documented ordering
// of its service-level lifecycleMu outer, Core.mu inner -- even though here
// both locks live on Core itself rather than split across two types.
type Core struct {
	lifecycleMu sync.Mutex

	mu        sync.RWMutex
	isRunning bool
	instance  *box.Box

	// ctx carries the protocol/inbound/outbound/endpoint/DNS-transport/
	// service registries. Built once in New and never reassigned, so it
	// needs no lock of its own.
	ctx context.Context
}

// New creates a Core with sing-box's standard protocol registries (the same
// ones github.com/sagernet/sing-box/cmd/sing-box registers) already bound
// into its context. No Box is running yet -- call Start to build one from a
// config.
func New() *Core {
	return &Core{ctx: include.Context(context.Background())}
}

// IsRunning reports whether a Box is currently running.
func (c *Core) IsRunning() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.isRunning
}

// running returns the live Box, or ErrNotRunning. Callers take a single
// RLock so they can never observe isRunning true with a nil instance.
func (c *Core) running() (*box.Box, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if !c.isRunning || c.instance == nil {
		return nil, ErrNotRunning
	}
	return c.instance, nil
}

// Start builds a Box from the given sing-box JSON config and starts it. The
// build and the start itself happen outside mu -- binding listeners can
// take real time -- only the final publish of the instance is locked, so a
// half-built Box is never observable via IsRunning/running.
//
// Start fails if a Box is already running; call Stop first.
func (c *Core) Start(configJSON []byte) error {
	c.lifecycleMu.Lock()
	defer c.lifecycleMu.Unlock()

	if c.IsRunning() {
		return errors.New("singbox-core: already running")
	}

	var opt option.Options
	if err := opt.UnmarshalJSONContext(c.ctx, configJSON); err != nil {
		return fmt.Errorf("unmarshal config: %w", err)
	}

	instance, err := box.New(box.Options{
		Context: c.ctx,
		Options: opt,
	})
	if err != nil {
		return fmt.Errorf("build box: %w", err)
	}

	if err := instance.Start(); err != nil {
		_ = instance.Close()
		return fmt.Errorf("start box: %w", err)
	}

	c.mu.Lock()
	c.instance = instance
	c.isRunning = true
	c.mu.Unlock()
	return nil
}

// Stop detaches the instance under lifecycleMu+mu and closes it outside mu,
// since Box.Close tears down every manager subsystem in sequence and can
// take a moment. Stop on an already-stopped Core is a no-op, not an error.
func (c *Core) Stop() error {
	c.lifecycleMu.Lock()
	defer c.lifecycleMu.Unlock()

	c.mu.Lock()
	instance := c.instance
	c.instance = nil
	c.isRunning = false
	c.mu.Unlock()

	if instance == nil {
		return nil
	}
	return instance.Close()
}
