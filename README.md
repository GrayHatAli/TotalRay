# TotalRay

TotalRay turns **any Debian-based Linux box** — a Raspberry Pi, an old
laptop repurposed as a home server, or a cloud VPS — into a self-managing
VPN gateway or remote proxy, built on [sing-box](https://sing-box.sagernet.org/).

It manages a pool of subscription-sourced server configs, continuously
tests them against real connectivity (not just ping), keeps only servers
that actually work in front of client traffic, and (when it's sitting on
your own LAN) automatically routes domestic (Iranian) traffic direct
while tunneling everything else.

## Two deployment modes

TotalRay runs the exact same engine either way — subscription fetching,
pool testing, scoring — the only difference is how devices reach it:

- **LAN gateway** — the classic setup: install it on a box that sits on
  your home network (a Raspberry Pi, an old PC, anything). It becomes the
  LAN's default gateway and transparently intercepts/tunnels traffic for
  every connected device — **zero configuration on the devices
  themselves**.
- **Remote proxy server** — no LAN required. Install it on a cloud VPS
  (or any Linux box reachable over the internet) and your devices
  connect *to it* over the internet, using one stable SOCKS5/HTTP proxy
  endpoint (`lan_proxy` in `config.yaml`), instead of installing and
  periodically swapping v2ray-style client configs on each device. The
  endpoint you connect to never changes — TotalRay keeps rotating through
  the actual upstream subscription servers behind it.

Pick the mode at install time (`sudo bash scripts/install.sh --mode=gateway`
or `--mode=proxy`) — see [Installation](#installation).

## Features

- **No per-device setup (gateway mode).** The box becomes the LAN's
  default gateway; every connected device is transparently routed,
  tunneled, and (for Iranian destinations) sent direct — with zero
  configuration on the device itself.
- **One stable endpoint instead of rotating configs (proxy mode).** Point
  every device's system (or per-app) SOCKS5/HTTP proxy settings at the
  server once. TotalRay handles finding, testing, and swapping the
  actual upstream server behind that endpoint — devices never touch a
  config again.
- **Dual-pool config testing.** Every fetched server starts in the
  *candidate pool* (pool A). Only servers that pass a real connectivity
  test (an actual HTTP request, not ICMP ping) under a latency threshold
  graduate to the *verified pool* (pool B). **The active proxy group is
  built exclusively from pool B** — end users are never routed through an
  untested or already-known-bad server.
  - Pool A is re-tested every `pool_a_test_minutes` (default 15).
  - Pool B is re-tested every `pool_b_test_minutes` (default 3), so a
    server going bad is caught quickly.
  - A server has to fail **twice in a row** before it's actually pulled
    out of pool B (`pool_b_demote_grace`). This absorbs one-off latency
    blips without restarting sing-box for nothing — see "Why does
    sing-box restart so rarely" below.
  - A server that keeps failing is fully removed once its score drops to
    `fail_threshold` (default -5), regardless of which pool it was in
    when that happened.
- **Real-time failover on packet drops (`live_monitor`).** Separate from
  the periodic pool-B test rounds, a background thread polls the Clash
  API every `check_interval_seconds` (default 2s) for the active
  connection's health. If it sees `error_threshold` (default 3) errors
  within a 10-second window, it immediately switches to the next-best
  pool-B server — no waiting for the next scheduled test round. A
  `cooldown_seconds` (default 60s) floor between failovers keeps it from
  thrashing. This is what catches mid-stream/mid-call drops that a
  3-minute test cycle would otherwise leave you sitting on for minutes.
- **Domestic traffic stays direct (gateway mode).** Iranian domains/IPs
  ([Chocolate4U/Iran-sing-box-rules](https://github.com/Chocolate4U/Iran-sing-box-rules))
  are routed directly instead of through the tunnel. This works via
  sing-box's rule-action-based TLS/HTTP sniffing, so it's correct
  regardless of which DNS server a client actually uses — it doesn't
  depend on the client asking *this* box for DNS. (In proxy mode this
  still applies to the box's own upstream connections, but since a
  remote VPS is already outside the LAN, whether it's worth keeping on
  depends on your setup — see `routing.iran_direct` in the config
  reference.)
- **The gateway's own traffic never depends on the tunnel it's building.**
  Subscription and rule-set fetching happens with a direct-first
  strategy: sockets (including the DNS lookup itself, done with a small
  from-scratch resolver) are tagged with sing-box's own
  `auto_redirect_output_mark`, the same mechanism sing-box uses to keep
  its own upstream connections from looping back into its own tunnel. If
  a direct attempt still fails (a host that's genuinely only reachable
  through the tunnel), it falls back to an explicit local SOCKS/HTTP
  proxy (`local_proxy.port`, default 2080). Without this, updating
  subscriptions after the gateway takes over the network is a
  chicken-and-egg problem: the update needs the tunnel, and the tunnel's
  DNS needs a working server list.
- **Per-subscription custom headers.** Some subscription panels only
  accept requests that look exactly like one specific app (not just "a
  known VPN client's User-Agent"). A subscription entry can override the
  entire header set — see `config.yaml`'s commented example.
- **Real-connectivity `status`.** `totalray status` doesn't just report
  what sing-box *thinks* is selected — it queries sing-box's Clash API for
  the currently active server, independently checks the apparent public
  exit IP, and confirms they match.

## How it fits together

```
subscriptions (URLs) --fetch--> pool A (candidates) --test--> pool B (verified)
                                                                    |
                                                          sing-box outbound group
                                                          (urltest picks the fastest)
                                                                    |
                        +-------------------------------+-------------------------------+
                  gateway mode:                  proxy mode:                  TotalRay itself
              LAN clients (transparent      remote clients (SOCKS5/HTTP    (direct-first, see above)
                 TUN redirect)                via `lan_proxy`, over WAN)
```

- **`sing-box`** does the actual packet interception (`auto_route` +
  `auto_redirect` on a TUN interface, gateway mode) and/or proxying
  (`lan_proxy`, either mode). It runs as its own systemd service
  (`sing-box.service`, from the upstream package) and is fully
  config-driven — TotalRay writes `/etc/sing-box/config.json` and
  restarts it only when the active server set actually changes (see
  below for why that matters).
- **`totalray`** (this project) is the Python management layer:
  fetching/parsing subscriptions, testing servers, scoring, and building
  the sing-box config. It runs as `totalray.service`.
- **`dnsmasq`** (gateway mode only) can serve DHCP/DNS to the LAN,
  pointing clients at this box. Not installed/used in proxy mode.

### Why does sing-box restart so rarely?

sing-box has a known bug
([SagerNet/sing-box#3572](https://github.com/SagerNet/sing-box/issues/3572))
where frequent restarts of `auto_redirect` can leave stale kernel routes
behind, eventually causing it to crash-loop (`append ipv4 loopback route:
file exists`). Two things in TotalRay specifically guard against this:

1. `rebuild_and_apply()` only restarts sing-box when the actual set of
   server tags in the active group changed — not on every test round.
2. Pool-B demotion requires `pool_b_demote_grace` (default 2) consecutive
   failures, so a single latency blip doesn't churn group membership (and
   therefore doesn't trigger a restart) for nothing.

If you ever do see the crash-loop, a full reboot clears the stale kernel
state; restarting the service again immediately tends to make it worse,
not better.

### Live monitor vs. pool-B testing — which one reacts to what

These two mechanisms are complementary, not redundant:

- **Pool-B test rounds** (every `pool_b_test_minutes`, default 3) verify
  that servers *can* be connected to at all, and demote/remove ones that
  can't (with `pool_b_demote_grace` absorbing one-off blips).
- **`live_monitor`** doesn't test connectivity independently — it watches
  the *currently active* connection's real error rate while it's in use,
  so it reacts within seconds to something going wrong mid-session (a
  server that was fine 90 seconds ago but just started dropping
  packets), instead of waiting for the next scheduled test round.

If both are enabled (the default), a degrading server usually gets
caught by `live_monitor` first; the pool-B round afterwards is what
actually demotes it out of the verified pool.

## Requirements

- Any Debian-based Linux box — Raspberry Pi, an old laptop, a cloud VPS,
  anything with `apt` — arm64, armhf, or amd64.
  - **Gateway mode** needs two network paths: one to the LAN it will
    serve, one to the internet (can be the same interface if the box
    sits between the LAN and the router).
  - **Proxy mode** just needs one public network path — this is the
    normal setup for a cloud VPS.
- Debian/Raspberry Pi OS with NetworkManager-backed netplan.
- Python 3.11+, `sing-box` 1.13+ (installed automatically by
  `scripts/install.sh`).

## Installation

```bash
git clone https://github.com/GrayHatAli/TotalRay.git
cd TotalRay
sudo bash scripts/install.sh --mode=gateway   # on your own LAN (default)
# or
sudo bash scripts/install.sh --mode=proxy     # on a remote/cloud VPS
```

Omit `--mode` and the installer will ask interactively (defaulting to
`gateway`); set `TOTALRAY_MODE=proxy` instead of the flag if you're
scripting the install non-interactively.

This installs sing-box, copies the `totalray` package to `/opt/totalray`,
creates a venv, installs a systemd unit (`totalray.service`) and a CLI
wrapper (`/usr/local/bin/totalray`). In proxy mode it also skips the
DHCP/dnsmasq setup (there's no LAN to serve) and generates a random
`lan_proxy` password instead of leaving the config's placeholder in
place — the installer prints the connection details (server IP, port,
username, password) at the end.

**Config file:** the CLI and the systemd service both default to
`/etc/totalray/config.yaml` (this lives next to the code rather than
under `/etc` — if you'd prefer FHS-style separation, symlink it and pass
`--config` explicitly, or edit both the service unit and `main.py`'s
`--config` default consistently. Keeping them in sync matters: pointing
the CLI and the service at two different config files means `totalray
status` will show a different database than what's actually running.)

Add subscriptions by editing `subscriptions:` in `config.yaml` (plain
URLs or `{name, url, headers}` objects — see the commented example),
then:

```bash
sudo systemctl restart totalray
sudo totalray status
```

## Network setup

Three ways to get devices routed through TotalRay, depending on mode:

**Gateway mode, option A - this box runs DHCP.** Turn off the router's
own DHCP server, let this box's `dnsmasq` hand out leases (gateway + DNS
= this box's IP). Simplest, most airtight, but means this box's DHCP has
to be reliable.

**Gateway mode, option B - router keeps DHCP, only its advertised
gateway changes.** Give this box a static IP outside the router's DHCP
pool, then change the router's **Default Gateway** field (not the
router's own LAN IP) to point at this box. The router still hands out
addresses and DNS as before; clients just get redirected here as their
gateway. This is less disruptive to change but means the router's own
DNS answers are used by clients — which is fine, because Iranian-domain
routing here works via TLS/HTTP sniffing, not by depending on this box
seeing the DNS query (see Features above).

**Proxy mode - point devices at the server's public IP.** No gateway/DHCP
changes at all. On each device, set the system (or per-app) SOCKS5/HTTP
proxy to `<server-public-ip>:2081` with the credentials from
`lan_proxy` in `config.yaml` (the installer prints these after setup).
Make sure the port is allowed through the VPS provider's cloud
firewall/security group — the installer only configures the box itself,
not provider-side network rules.

With gateway mode, `auto_redirect` handles NAT/forwarding automatically
— no manual `iptables`/`nftables` rules needed.

Before flipping the gateway for the whole LAN (gateway mode) or sharing
the proxy endpoint with your devices (proxy mode), confirm on the box
itself first:
```bash
sudo systemctl status sing-box totalray   # both active, no restart-looping
sudo totalray status                       # pool B (verified) > 0, status: connected
curl https://api.ipify.org                 # returns the tunnel's exit IP, not the box's real one
```
If sing-box is unstable or pool B is empty, flipping the gateway will
cut off the whole LAN's internet (not just filtering) until it's
fixed — the box becomes a dead end, not just an unfiltered pass-through.
In proxy mode an empty pool B just means the proxy endpoint has nothing
to forward through yet.

## Configuration reference (`config.yaml`)

| Section | Key | Meaning |
|---|---|---|
| `subscriptions` | - | List of subscription URLs (string or `{name, url, headers}`) |
| `schedule` | `sub_update_minutes` | How often subscriptions are re-fetched |
| | `pool_a_test_minutes` / `pool_b_test_minutes` | Test round intervals |
| | `rules_update_hours` | Iran rule-set refresh interval |
| `test` | `ping_threshold_ms` | Max latency to count as healthy |
| | `fail_threshold` | Score at which a config is permanently removed |
| | `pool_b_demote_grace` | Consecutive failures before leaving pool B |
| | `chunk_size` | Configs per temporary test sing-box instance - lower this on low-RAM boards (Pi 2/3/Zero, small VPS) |
| `proxy_group` | `urltest_interval`/`urltest_tolerance` | sing-box's own live re-test of the active group |
| `live_monitor` | `enabled` | Real-time failover on packet drops, independent of pool-B rounds |
| | `check_interval_seconds` | How often the active connection's health is polled (default 2s) |
| | `error_threshold` | Errors within a 10s window that trigger an immediate failover (default 3) |
| | `cooldown_seconds` | Minimum time between live-monitor failovers (default 60s) |
| `routing` | `iran_direct` | Route Iranian domains/IPs direct |
| | `custom_rules` | Extra routing rules (domain/IP matchers to direct or select) |
| `dns` | `remote_server` | DoH resolver used for non-Iranian domains (must be an IP) |
| | `local_server` | Upstream DNS used for Iranian domains and TotalRay's own direct-first fetching (the router's IP in gateway mode, this box's own resolver in proxy mode - filled in automatically at install) |
| `tun` | `interface`, `stack`, `mtu` | TUN interface settings (gateway mode) |
| `clash_api` | `listen`, `secret` | sing-box's Clash-compatible API, used by `totalray status` |
| `local_proxy` | `port` | Explicit local SOCKS/HTTP proxy (fallback path for TotalRay's own fetches) |
| `lan_proxy` | `listen`, `port`, `username`, `password` | Client-facing SOCKS5+HTTP proxy. In gateway mode this is an optional extra way to reach the tunnel without touching the network gateway; in proxy mode it **is** the product - point devices at `<server-ip>:<port>` |
| `paths` | - | Where the database, rule-sets, and generated sing-box config live |

## CLI reference

```
totalray init            create the database and directories
totalray add-sub <url>   add a subscription
totalray del-sub <id>    remove a subscription
totalray list            show subscriptions and stats
totalray update-subs     manually refresh subscriptions
totalray update-rules    refresh the Iran rule-sets
totalray test-a          run one pool-A (candidate) test round
totalray test-b          run one pool-B (verified) test round
totalray build           build and apply the sing-box config
totalray run             run the scheduler daemon (this is what the service runs)
totalray status          real connectivity status + pool overview + top/worst configs
totalray failed-requests show recent failed HTTP requests
```

## Updating an installed instance

The installer places an update command at `/usr/local/sbin/totalray-update`.
All date/time values shown by `totalray status` use the `Asia/Tehran`
timezone.
It downloads the latest `main` branch, updates the application and Python
dependencies, reloads systemd, and restarts `totalray.service`.

```bash
sudo totalray-update
sudo totalray status
```

The update does **not** replace `/etc/totalray/config.yaml`, `/var/lib/totalray`,
or `/etc/sing-box`; subscriptions, scores, databases, rule-sets, and generated
sing-box state are preserved. It updates `/opt/totalray` because the service's
Python environment and application code live there.

## Services & logs

```bash
sudo systemctl status sing-box totalray
journalctl -u totalray -f
journalctl -u sing-box -f
```

- Data & logs: `/var/lib/totalray/` (`totalray.log`, rotated)
- Failed HTTP requests (subscriptions/rule-sets): JSON-lines at
  `/var/lib/totalray/totalray_failed_requests.log`
- Generated sing-box config: `/etc/sing-box/config.json`

## Troubleshooting

- **`totalray status` shows 0 configs but the service is clearly
  running.** The CLI and the service are reading different config files
  - check `ExecStart` in `/etc/systemd/system/totalray.service` against
  the `--config` default in `main.py`/what you pass on the command line.
- **Subscription updates fail with DNS resolution errors right after the
  gateway starts working.** This is the chicken-and-egg problem described
  above - it should be handled automatically by the direct-first fetch
  logic; if it isn't, check that pool B isn't empty (an empty pool B
  means there's nothing for the local proxy fallback to route through
  either).
- **sing-box crash-loops with `file exists` after several restarts.**
  See "Why does sing-box restart so rarely" above - reboot rather than
  repeatedly restarting the service.
- **A subscription always returns HTTP 501 or similar, but works fine in
  a phone app.** Some panels check for an exact client fingerprint
  (specific headers, not just User-Agent). Capture the working app's
  request (e.g. with a MITM proxy) and set it as that subscription's
  `headers:` override.
- **(Proxy mode) devices can't reach the proxy at all.** Check the VPS
  provider's cloud firewall/security group allows inbound TCP on the
  `lan_proxy.port` (default 2081) - `scripts/install.sh` only configures
  the box's own OS, not provider-side network rules.

## Development

```bash
python3 -m py_compile totalray/*.py     # syntax check
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m totalray --config config.yaml status
```

No CI is configured in this repository yet; if you add one, run the two
commands above as the build/test steps.
