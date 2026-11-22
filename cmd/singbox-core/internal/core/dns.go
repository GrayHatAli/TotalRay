package core

import (
	"fmt"

	"github.com/sagernet/sing-box/adapter"
	"github.com/sagernet/sing-box/option"
	"github.com/sagernet/sing/service"
)

// ReloadDNSServer re-creates one DNS server on the live Box from a single
// sing-box DNS server JSON object (the same shape as one entry of a config's
// "dns.servers" array), without restarting anything.
//
// Why this exists: a DNS server with a "detour" resolves that detour outbound
// ONCE (sing-box's DetourDialer caches it behind a sync.Once) and keeps the
// object for the life of the DNS transport. The Python side replaces the
// "auto"/"select" groups at runtime (remove + add, same tags); route rules look
// the tag up per connection and follow the new groups, but a DNS server that
// detours through "select" stays bound to the original, now-retired chain and
// stops resolving as soon as that chain's leaf dies. Re-creating the server
// after the groups are replaced binds its dialer to the current outbounds.
//
// adapter.DNSTransportManager.Create starts the new transport and closes and
// replaces any existing transport with the same tag; the DNS router resolves
// transports per query, so the swap takes effect on the next lookup.
//
// Returns ErrNotRunning if no Box is running.
func (c *Core) ReloadDNSServer(configJSON []byte) error {
	b, err := c.running()
	if err != nil {
		return err
	}

	var server option.DNSServerOptions
	if err := server.UnmarshalJSONContext(c.ctx, configJSON); err != nil {
		return fmt.Errorf("unmarshal dns server config: %w", err)
	}
	if server.Tag == "" {
		return fmt.Errorf("dns server config missing tag")
	}

	// The registry in c.ctx is the one box.New registered the running Box's
	// managers into (Core builds its context with a registry up front, so
	// box.New reuses it rather than creating its own).
	manager := service.FromContext[adapter.DNSTransportManager](c.ctx)
	if manager == nil {
		return fmt.Errorf("dns transport manager is unavailable")
	}

	return manager.Create(
		c.ctx,
		b.LogFactory().NewLogger("dns/"+server.Type+"["+server.Tag+"]"),
		server.Tag,
		server.Type,
		server.Options,
	)
}
