# TotalRay

TotalRay turns a Raspberry Pi into a transparent VPN gateway for a home
network. Devices on the LAN get filtered/tunneled internet **without
installing any client software** — the Pi intercepts and routes traffic
itself, using [sing-box](https://sing-box.sagernet.org/) under the hood.

It manages a pool of subscription-sourced server configs, continuously
tests them against real connectivity (not just ping), keeps only servers
that actually work in front of client traffic, and automatically routes
domestic (Iranian) traffic direct while tunneling everything else.

## Features

- **No per-device setup.** The Pi becomes the LAN's default gateway; every
  connected device is transparently routed, tunneled, and (for Iranian
  destinations) sent direct — with zero configuration on the device
  itself.
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
- **Domestic traffic stays direct.** Iranian domains/IPs
  ([Chocolate4U/Iran-sing-box-rules](https://github.com/Chocolate4U/Iran-sing-box-rules))
  are routed directly instead of through the tunnel. This works via
  sing-box's rule-action-based TLS/HTTP sniffing, so it's correct
  regardless of which DNS server a client actually uses — it doesn't
  depend on the client asking *this* box for DNS.
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
                                                    +---------------+---------------+
                                              LAN clients                    TotalRay itself
                                        (transparent TUN redirect)      (direct-first, see above)
```

- **`sing-box`** does the actual packet interception (`auto_route` +
  `auto_redirect` on a TUN interface) and proxying. It runs as its own
  systemd service (`sing-box.service`, from the upstream package) and is
  fully config-driven — TotalRay writes `/etc/sing-box/config.json` and
  restarts it only when the active server set actually changes (see
  below for why that matters).
- **`totalray`** (this project) is the Python management layer:
  fetching/parsing subscriptions, testing servers, scoring, and building
  the sing-box config. It runs as `totalray.service`.
- **`dnsmasq`** (optional, depending on network setup — see below) can
  serve DHCP/DNS to the LAN, pointing clients at the Pi.

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

## Requirements

- Raspberry Pi (or any Debian-based Linux box) with two network paths:
  one to the LAN it will serve, one to the internet (can be the same
  interface if the Pi sits between the LAN and the router).
- Debian/Raspberry Pi OS with NetworkManager-backed netplan.
- Python 3.11+, `sing-box` 1.13+ (installed automatically by
  `scripts/install.sh`).

## Installation

```bash
git clone https://github.com/GrayHatAli/TotalRay.git
cd TotalRay
sudo bash scripts/install.sh
```

This installs sing-box, copies the `totalray` package to `/opt/totalray`,
creates a venv, installs a systemd unit (`totalray.service`) and a CLI
wrapper (`/usr/local/bin/totalray`).

**Config file:** the CLI and the systemd service both default to
`/opt/totalray/config.yaml` (this lives next to the code rather than
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

## Network setup: getting LAN traffic to the Pi

Two ways to make the Pi the LAN's gateway, without any extra hardware:

**Option A - the Pi runs DHCP.** Turn off the router's own DHCP server,
let the Pi's `dnsmasq` hand out leases (gateway + DNS = the Pi's IP).
Simplest, most airtight, but means the Pi's DHCP has to be reliable.

**Option B - router keeps DHCP, only its advertised gateway changes.**
Give the Pi a static IP outside the router's DHCP pool, then change the
router's **Default Gateway** field (not the router's own LAN IP) to point
at the Pi. The router still hands out addresses and DNS as before;
clients just get redirected to the Pi as their gateway. This is less
disruptive to change but means the router's own DNS answers are used by
clients — which is fine, because Iranian-domain routing here works via
TLS/HTTP sniffing, not by depending on the Pi seeing the DNS query (see
Features above).

With either option, `auto_redirect` handles NAT/forwarding automatically
— no manual `iptables`/`nftables` rules needed.

Before flipping the gateway for the whole LAN, confirm on the Pi
itself first:
```bash
sudo systemctl status sing-box totalray   # both active, no restart-looping
sudo totalray status                       # pool B (verified) > 0, status: connected
curl https://api.ipify.org                 # returns the tunnel's exit IP, not the Pi's real one
```
If sing-box is unstable or pool B is empty, flipping the gateway will
cut off the whole LAN's internet (not just filtering) until it's
fixed — the Pi becomes a dead end, not just an unfiltered pass-through.

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
| | `chunk_size` | Configs per temporary test sing-box instance - lower this on low-RAM boards (Pi 2/3/Zero) |
| `proxy_group` | `urltest_interval`/`urltest_tolerance` | sing-box's own live re-test of the active group |
| `routing` | `iran_direct` | Route Iranian domains/IPs direct |
| | `custom_rules` | Extra routing rules (domain/IP matchers to direct or select) |
| `dns` | `remote_server` | DoH resolver used for non-Iranian domains (must be an IP) |
| | `local_server` | Router's DNS, used both for Iranian domains and TotalRay's own direct-first fetching |
| `tun` | `interface`, `stack`, `mtu` | TUN interface settings |
| `clash_api` | `listen`, `secret` | sing-box's Clash-compatible API, used by `totalray status` |
| `local_proxy` | `port` | Explicit local SOCKS/HTTP proxy (fallback path for TotalRay's own fetches) |
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

## Development

```bash
python3 -m py_compile totalray/*.py     # syntax check
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m totalray --config config.yaml status
```

No CI is configured in this repository yet; if you add one, run the two
commands above as the build/test steps.
