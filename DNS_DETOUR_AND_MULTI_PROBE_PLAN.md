# DNS Detour Rebinding + Multi-Destination Health Plan

## Why this plan exists

Incident of 2026-11-20 (evening). Two independent defects were found while
investigating a gateway outage:

1. **The embedded sing-box DNS resolver silently dies after enough live group
   replacements.** `dns.servers[remote]` is an HTTPS server with
   `detour: "select"`. sing-box 1.13.14 (`common/dialer/detour.go`) wraps that
   detour in a `DetourDialer` whose `initOnce` resolves the outbound object
   **once** and caches it for the life of the DNS transport. `StandbyPool.reconcile`
   replaces the `auto` and `select` groups (remove + add, same tags) whenever
   pool membership changes (15 times on 2026-11-20 alone). Route rules look the
   tag up per connection, so traffic follows the new groups, but the DNS
   transport keeps the *original* `select` -> `auto` -> leaf chain. When that
   original leaf died (21:07:59), every query to the embedded resolver began to
   time out (`dns: exchange failed ... dial tcp 54.37.240.40:8080: i/o timeout`,
   ~9,400 errors in hour 21 and ~7,700 in hour 22, still ongoing at 23:37).
2. **Health is judged against a single destination.** Pool tests, standby
   probes and the live monitor all use `https://www.gstatic.com/generate_204`.
   A server that reaches Google but not Cloudflare or Telegram counts as
   "verified". Measured on the Pi: of the three live outbounds, one reached
   Google + Telegram web + Cloudflare, one reached only Google, one reached
   only Cloudflare. A one-URL ping therefore says nothing about the real
   traffic mix (Telegram, push services, CDNs).

Evidence and source references live in the incident analysis; the fixes are
scoped below. This plan follows the same phase format as
`SINGBOX_CORE_EMBED_PLAN.md`.

## Design Principles

1. **Do not change what already works.** Routing, TUN, inbounds, the Clash API
   and the selector-based failover stay as they are. Phase 1 only recreates
   DNS transports; Phase 3 only widens what "healthy" means.
2. **Fix the binding, not the symptom.** A restart clears the stale DNS chain
   for hours; recreating the DNS transport after each group replacement fixes
   it for good and needs no restart.
3. **Be generic.** Re-bind every DNS server whose `detour` is one of the
   replaced group tags, taken from the desired config, instead of hard-coding
   the tag `remote`.
4. **Fail soft.** A failed DNS re-bind must be logged and reported but must not
   undo or block a successful outbound reconcile.
5. **Strict only where it is cheap.** Multi-destination checks run for Pool B
   rounds and standby probes (tens of servers), never for the thousands of
   Pool A candidates, and a server that fails the primary URL is rejected
   without running the extra URLs.

---

# Phase 1 - Go core: recreate a DNS server on the live Box

## Goal

Let the daemon replace a running DNS transport in place so that its detour
dialer binds to the *current* outbound objects.

## Tasks

- [x] `core.ReloadDNSServer(configJSON []byte) error`: unmarshal one entry of
      the config's `dns.servers` array (`option.DNSServerOptions`) with the core
      context, take `adapter.DNSTransportManager` from `service.FromContext`
      on `c.ctx` (the registry is shared with the running Box), and call
      `Create(ctx, logger, tag, type, options)`. `TransportManager.Create`
      starts the new transport and closes and replaces any existing transport
      with the same tag, so queries pick it up on the next lookup
      (`dns/router.go` resolves transports per query).
- [x] Reject an empty tag and return `ErrNotRunning` when no Box is running.
- [x] Control protocol: new command `reload_dns_server` with `config` (one DNS
      server object), documented in `protocol.go`, dispatched in `server.go`.
- [x] Go tests: command round trip with a fake core; core-level test that a
      DNS server detouring through a selector keeps resolving after that
      selector (and its leaf) are replaced and the DNS server is reloaded.

## Acceptance Criteria

- [x] `go vet` and `go test` pass with the production build tags.
- [x] The core-level test fails without the reload call and passes with it
      (it must demonstrate the stale binding, not merely call the function).
- [x] No other control command changes behaviour.

## Suggested Commit

`feat(singbox-core): reload_dns_server command to rebind a DNS transport to the current outbounds`

---

# Phase 2 - Python: rebind DNS after group replacement

## Goal

Call the new command whenever `StandbyPool.reconcile` replaces `auto`/`select`.

## Tasks

- [x] `SingboxCoreClient.reload_dns_server(config)`.
- [x] In `StandbyPool.reconcile`, after the groups are re-added and **before**
      stale leaves are removed, reload every DNS server in `desired["dns"]["servers"]`
      whose `detour` is in `GROUP_TAGS`. Errors are logged as warnings and
      appended to the returned summary; they never raise.
- [x] Reconcile summary mentions the number of DNS servers rebound.
- [x] Unit tests (FakeCore): rebind happens only when groups changed, only for
      servers with a group detour, after the group re-add and before leaf
      removal, and a failing reload does not fail the reconcile.

## Acceptance Criteria

- [x] Existing `test_singbox_embed.py` tests still pass unchanged.
- [x] Full suite passes: `.venv_test/bin/python -m pytest tests/ -q`.

## Suggested Commit

`fix(standby-pool): rebind detour DNS servers after replacing auto/select so DNS follows the live chain`

---

# Phase 3 - Multi-destination health

## Goal

Treat a server as healthy only if it reaches a majority of a small, fixed set
of destinations, so Google-only servers stop qualifying as "verified".

## Tasks

- [ ] Settings: `test.extra_urls` (default: `https://cp.cloudflare.com/generate_204`,
      `https://web.telegram.org/`) and `test.min_destinations_ok` (default `2`,
      counted over primary + extra URLs). An empty `extra_urls` restores the
      old single-URL behaviour exactly.
- [ ] `tester.py`: new `measure_multi` used by Pool B rounds only. Primary URL
      first (failure returns -1 immediately); extra URLs run only for servers
      that passed it; result is the primary delay when at least
      `min_destinations_ok` destinations succeeded, else -1. Pool A keeps the
      single-URL path.
- [ ] `standby_probe.py`: probe all configured URLs for a tag (in parallel,
      bounded by the existing timeout); a tag is ready only when enough URLs
      succeeded. Record per-URL results in `snapshot()` for `status`.
- [ ] `live_monitor.create_monitor` passes the URLs and threshold to
      `StandbyProbe`.
- [ ] Config template: document the new keys in `config.yaml`.
- [ ] Tests: tester (all pass / primary fails / 1-of-3 fails the 2-of-3 rule /
      empty extras = old behaviour), probe (ready only at threshold, partial
      failure recorded), settings defaults.

## Acceptance Criteria

- [ ] With `extra_urls: []` every existing test passes unmodified.
- [ ] Pool A rounds issue no extra requests (asserted in a test).
- [ ] Full suite passes.

## Suggested Commit

`feat(health): require a majority of destinations (google, cloudflare, telegram) for pool B and standby probes`

---

# Phase 4 - Build, deploy, verify live

## Goal

Ship both fixes to the Pi and prove them on the real gateway.

## Tasks

- [ ] Cross-compile `singbox-core` for linux/arm (GOARM=7) with the Makefile
      tags; `scripts/pre_deploy_check.sh` passes.
- [ ] Offline deploy with `TOTALRAY_SRC_DIR` + `TOTALRAY_PREBUILT_CORE`
      (`scripts/update.sh`); record before/after `md5sum` of deployed files.
- [ ] Verify DNS: a query to the embedded resolver (`127.0.0.1:1053`) returns an
      answer; zero new `dial tcp 54.37.240.40:8080` errors after the restart.
- [ ] Verify it **survives a group replacement**: after the next
      `live pool reconciled` event the resolver still answers and no stale-leaf
      DNS errors appear.
- [ ] Verify multi-destination health: pool B round logs, `totalray status`
      probe snapshot shows per-URL results, and the live group members all pass
      the majority rule.

## Acceptance Criteria

- [ ] All three services active; exit IP works through the HTTP proxy.
- [ ] The two verification observations above recorded in the commit message
      or the plan.

## Suggested Commit

`docs(plan): record live verification of DNS rebinding and multi-destination health`
