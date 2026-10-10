# singbox-core

The embedded Go daemon from `SINGBOX_CORE_EMBED_PLAN.md`: a standalone Go daemon embedding
sing-box (v1.13.14, pinned to match the Pi's stock binary) as a library,
controlled over a local Unix socket. `totalray/builder.py` uses it for
routine pool reconciliation, while structural/rule changes retain the full
restart fallback. Its default socket path (`/run/totalray/singbox-core.sock`)
doesn't collide with the Clash API.

## Build / test

```sh
# sing-box v1.13.14 requires Go >= 1.24.7; if `go version` is older,
# `brew install go@1.24` and prefix commands with:
#   export PATH="/opt/homebrew/opt/go@1.24/bin:$PATH"
make build   # -> ./singbox-core
make test
make vet
```

The installer builds the daemon and installs it as
`/usr/local/libexec/totalray/singbox-core`. `systemd/sing-box.service` starts
it with `/etc/sing-box/config.json`; the service name stays `sing-box` so
existing systemctl and journal tooling remains compatible.

Build tags (`make` already passes these) mirror the protocol/feature set
the Pi's stock sing-box binary ships with: `with_quic`, `with_wireguard`,
`with_utls`, `with_acme`, `with_gvisor`, `with_clash_api`.
`with_reality_server` must **not** be passed -- sing-box v1.13.14 merged it
into `with_utls` and fails the build if it's given separately.

## Control protocol

Unix socket, newline-delimited JSON, one request per line, one response
per line, connection stays open for multiple requests:

```
-> {"cmd":"start","config":{...}}        (a full sing-box config)
<- {"ok":true}

-> {"cmd":"add_outbound","config":{...}} (one outbound object)
<- {"ok":true}

-> {"cmd":"remove_outbound","tag":"..."}
<- {"ok":true}

-> {"cmd":"is_running"}
<- {"ok":true,"running":true}

-> {"cmd":"stop"}
<- {"ok":true}
```

Failure: `{"ok":false,"error":"..."}`.

## Manual verification (run 2026-09-22, against a throwaway local config)

Config: `mixed` inbound on `127.0.0.1:18081`, Clash API on `127.0.0.1:9095`,
outbounds `direct/primary` behind `selector/proxy` (default `primary`),
route: `mixed-in -> proxy`. No production files touched.

1. **Started** the daemon and the box via the control socket; confirmed
   `GET /proxies/proxy` showed `"all":["primary"]`.
2. **Opened a live connection** through it (`curl --socks5-hostname
   127.0.0.1:18081 https://httpbin.org/drip?duration=12&numbytes=120`) and
   confirmed it in `GET /connections` (chain `primary -> proxy`).
3. **While that connection was still open**, sent `add_outbound` for a
   second outbound (`direct/secondary`). Result: `{"ok":true}`, and
   `GET /connections` immediately after showed the *same* connection ID,
   completely undisturbed.
4. Checked whether `secondary` was now selectable on the `proxy` group:
   `GET /proxies/secondary` shows it exists standalone, but
   `GET /proxies/proxy` still shows `"all":["primary"]`, and
   `PUT /proxies/proxy {"name":"secondary"}` returned
   **`400 Selector update error: not found`**.
5. Sent `remove_outbound` for `secondary` (not referenced by any group):
   `{"ok":true}`, no effect on the (still-open) drip connection, which went
   on to finish naturally (`curl` exit 0, all 120 bytes received) --
   neither add nor remove touched it.
6. Sent `remove_outbound` for `primary` (still referenced by the `proxy`
   selector): sing-box's own dependency tracking refused it --
   `{"ok":false,"error":"outbound[primary] is depended by proxy"}` -- the
   box stayed running throughout (`is_running` -> `true`).
7. `stop` closed the box cleanly.

**Finding that changes a Phase-3 assumption:** a `selector`/`urltest`
group resolves its member list exactly once, in its own `Start()`, from
the static `outbounds` list in its config. `add_outbound` registers a new
outbound in the daemon's manager -- real, dialable, and safe to add/remove
without any restart -- but it does **not** retroactively become selectable
on an existing group; the Clash API `select` call for a dynamically-added
tag fails with `400`. Conversely, sing-box already refuses to
`remove_outbound` any tag a group still statically depends on, regardless
of whether that tag is the group's *current* selection -- so the "don't
remove a live/selected tag" caution Phase 1's plan calls for is partly
enforced by sing-box itself already.

Practical implementation in Phase 3: `totalray/standby_pool.py` adds new
leaf outbounds first, removes the dependent `select` and `auto` groups in
dependency order, recreates those groups under the same tags, and only then
retires stale leaves. The active member is moved to a ready standby before a
retired tag can be removed. This is zero-process-restart, with a short,
bounded group-replacement window.
