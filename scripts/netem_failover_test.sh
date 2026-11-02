#!/usr/bin/env bash
# End-to-end test for live failover (Phase 6 of SINGBOX_CORE_EMBED_PLAN.md).
#
# It degrades the ACTIVE outbound's network path with `tc netem` and checks
# what TotalRay does about it. Run it on the gateway (or a test box with the
# same layout), as root:
#
#   sudo bash scripts/netem_failover_test.sh preflight   # read-only checks
#   sudo bash scripts/netem_failover_test.sh switch      # baseline, degrade, switch, refill
#   sudo bash scripts/netem_failover_test.sh breaker     # repeated failures open the breaker
#   sudo bash scripts/netem_failover_test.sh all         # all of the above
#   sudo bash scripts/netem_failover_test.sh clear       # remove any netem rules (safe any time)
#
# It works in both modes of live_monitor.shadow_mode:
#   shadow: expects a logged recommendation and NO switch   (Phase 8, step 1)
#   live:   expects a real switch, refill and breaker       (Phase 8, step 2+)
# The breaker test needs live mode.
#
# THIS DEGRADES THE REAL GATEWAY. Only traffic to the active server's IP is
# impaired, but everything routed through that server suffers while it runs.
# Run it when nobody needs the connection. netem is removed on exit, on
# Ctrl-C, and by a watchdog after MAX_IMPAIR seconds even if this script dies.
#
# Traffic: the passive signals need traffic through the active server. By
# default the script generates it through the loopback proxy (local_proxy in
# config.yaml, no password). With LOAD=manual it does not, and you start a
# download on a LAN device yourself.
#
# Tunables (environment): NETEM_PROFILE, MAX_IMPAIR, WAIT_SWITCH, WAIT_REFILL,
# BASELINE_SECONDS, LOAD, LOAD_STREAMS, LOAD_URL, TOTALRAY_CONFIG, FORCE.
set -euo pipefail

CONFIG="${TOTALRAY_CONFIG:-/etc/totalray/config.yaml}"
DATA_DIR="${TOTALRAY_DATA_DIR:-/var/lib/totalray}"
SINGBOX_CONF="${SINGBOX_CONFIG:-/etc/sing-box/config.json}"
DB="${TOTALRAY_DB:-/var/lib/totalray/totalray.db}"
PY="${TOTALRAY_PY:-/opt/totalray/venv/bin/python}"
NETEM_PROFILE="${NETEM_PROFILE:-loss 25% delay 150ms 50ms}"
MAX_IMPAIR="${MAX_IMPAIR:-150}"          # watchdog: netem never stays longer than this
WAIT_SWITCH="${WAIT_SWITCH:-90}"         # how long to wait for a switch / recommendation
WAIT_REFILL="${WAIT_REFILL:-90}"
BASELINE_SECONDS="${BASELINE_SECONDS:-20}"
LOAD="${LOAD:-auto}"                     # auto = through the LAN proxy, manual = you do it
LOAD_STREAMS="${LOAD_STREAMS:-4}"
LOAD_URL="${LOAD_URL:-https://speed.cloudflare.com/__down?bytes=100000000}"
FORCE="${FORCE:-0}"

PASS=0
FAIL=0
IMPAIR_IF=""
WATCHDOG_PID=""
LOAD_FLAG=""
LOAD_PIDS=()
T_START=$SECONDS

info() { printf '    %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 2; }

# ---- the tc commands (also used by tests, with no root needed) ------------
plan_commands() {  # plan_commands IFACE PROFILE IP...
  local iface="$1" profile="$2"
  shift 2
  # Everything goes to band 1:2 by default (priomap all 1); only traffic that
  # matches a filter is sent to band 1:3, which carries the netem impairment.
  echo "tc qdisc replace dev $iface root handle 1: prio bands 3 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1"
  echo "tc qdisc replace dev $iface parent 1:3 handle 30: netem $profile"
  local n=0 ip
  for ip in "$@"; do
    n=$((n + 1))
    if [[ "$ip" == *:* ]]; then
      echo "tc filter add dev $iface protocol ipv6 parent 1: prio $((100 + n)) u32 match ip6 dst $ip/128 flowid 1:3"
    else
      echo "tc filter add dev $iface protocol ip parent 1: prio $n u32 match ip dst $ip/32 flowid 1:3"
    fi
  done
}

# ---- helpers ---------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

cfg() {  # cfg a.b.c [default]  -> value from config.yaml (booleans as true/false)
  local value
  value="$("$PY" - "$CONFIG" "$1" <<'PYEOF'
import sys, yaml
cur = yaml.safe_load(open(sys.argv[1])) or {}
for part in sys.argv[2].split("."):
    cur = cur.get(part) if isinstance(cur, dict) else None
if isinstance(cur, bool):
    print("true" if cur else "false")
elif cur is not None:
    print(cur)
PYEOF
)"
  if [ -n "$value" ]; then echo "$value"; else echo "${2:-}"; fi
}

setup_clash() {
  CLASH="$(cfg clash_api.listen 127.0.0.1:9090)"
  local secret
  secret="$(cfg clash_api.secret)"
  AUTH=()
  if [ -n "$secret" ]; then AUTH=(-H "Authorization: Bearer $secret"); fi
}
clash_get() { curl -fsS -m 4 "${AUTH[@]}" "http://$CLASH$1"; }
active_tag() { clash_get /proxies/auto | jq -r '.now'; }
group_tags() { clash_get /proxies/auto | jq -r '.all[]'; }

lm() {  # lm JQ_EXPR -> value from the live monitor status file
  jq -r "$1" "$DATA_DIR/live_monitor_status.json" 2>/dev/null || echo ""
}

server_of() { jq -r --arg t "$1" '[.outbounds[] | select(.tag==$t) | .server][0] // empty' "$SINGBOX_CONF"; }

ips_of() {  # a hostname or IP literal -> one IP per line
  if [[ "$1" =~ ^[0-9.]+$ || "$1" == *:* ]]; then echo "$1"; return; fi
  getent ahosts "$1" | awk '{print $1}' | sort -u
}

egress_if() {
  local route
  route="$(ip route get "$1" 2>/dev/null || true)"
  awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}' <<<"$route"
}

pool_b_count() {
  sqlite3 -readonly "$DB" "SELECT COUNT(*) FROM configs WHERE pool='b' AND removed=0;" 2>/dev/null || echo "?"
}

tunnel_sockets() {  # TCP sockets owned by sing-box that talk to a public address
  ss -tnpH 2>/dev/null | awk '/"sing-box"|"singbox-core"/ {print $5}' |
    grep -Ev '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|\[?(::1|fe80|fd|fc))' | wc -l
}

# ---- impairment ------------------------------------------------------------
clear_impair() {
  if [ -n "$IMPAIR_IF" ]; then
    tc qdisc del dev "$IMPAIR_IF" root 2>/dev/null || true
  fi
  if [ -n "$WATCHDOG_PID" ]; then kill "$WATCHDOG_PID" 2>/dev/null || true; WATCHDOG_PID=""; fi
}

impair() {  # impair IP...   (all IPs of one server)
  IMPAIR_IF="$(egress_if "$1")"
  [ -n "$IMPAIR_IF" ] || die "cannot find the egress interface for $1"
  local line words
  while IFS= read -r line; do
    read -r -a words <<<"$line"
    "${words[@]}" </dev/null
  done < <(plan_commands "$IMPAIR_IF" "$NETEM_PROFILE" "$@")
  nohup bash -c "sleep $MAX_IMPAIR; tc qdisc del dev $IMPAIR_IF root 2>/dev/null || true" >/dev/null 2>&1 &
  WATCHDOG_PID=$!
  info "netem on $IMPAIR_IF -> $* ($NETEM_PROFILE); auto-removed after ${MAX_IMPAIR}s"
}

# ---- traffic ---------------------------------------------------------------
start_load() {
  if [ "$LOAD" = "manual" ]; then
    info "LOAD=manual: start a download on a LAN device now and keep it running"
    return
  fi
  local port i
  port="$(cfg local_proxy.port 2080)"
  LOAD_FLAG="$(mktemp)"
  for i in $(seq 1 "$LOAD_STREAMS"); do
    (
      while [ -e "$LOAD_FLAG" ]; do
        curl -s -o /dev/null -m 25 -x "socks5h://127.0.0.1:$port" "$LOAD_URL" || true
      done
    ) &
    LOAD_PIDS+=($!)
  done
  info "load: $LOAD_STREAMS parallel downloads through the loopback proxy (port $port)"
}

stop_load() {
  if [ -n "$LOAD_FLAG" ]; then rm -f "$LOAD_FLAG"; LOAD_FLAG=""; fi
  local pid
  for pid in "${LOAD_PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  LOAD_PIDS=()
}

cleanup() {
  stop_load
  clear_impair
}

# ---- observation -------------------------------------------------------------
line_now() {  # one timeline line
  local vals degraded bad switches breaker hist
  vals="$(jq -r '[.passive_signal.degraded, .passive_signal.bad_samples, .failover_count,
                  .failover.circuit_open, (.failover.history | length)] | @tsv' \
          "$DATA_DIR/live_monitor_status.json" 2>/dev/null || echo "? ? ? ? ?")"
  read -r degraded bad switches breaker hist <<<"$vals"
  printf '    t+%3ss active=%-9s degraded=%-5s bad_samples=%-3s switches=%-2s breaker_open=%-5s history=%s\n' \
    $((SECONDS - T_START)) "$(active_tag 2>/dev/null || echo '?')" "$degraded" "$bad" "$switches" "$breaker" "$hist"
}

wait_until() {  # wait_until SECONDS COMMAND...  (polls every 2s, prints a timeline)
  local limit="$1" t0=$SECONDS
  shift
  while [ $((SECONDS - t0)) -lt "$limit" ]; do
    if "$@"; then return 0; fi
    line_now
    sleep 2
  done
  "$@"
}

# ---- checks ------------------------------------------------------------------
preflight() {
  step "preflight"
  [ "$(id -u)" -eq 0 ] || die "run as root"
  local c
  for c in tc ip ss jq curl getent awk sqlite3 systemctl; do need "$c"; done
  [ -x "$PY" ] || die "python not found at $PY (set TOTALRAY_PY)"
  [ -r "$CONFIG" ] || die "cannot read $CONFIG"
  modprobe sch_netem 2>/dev/null || true
  setup_clash

  systemctl is-active --quiet totalray && ok "totalray.service is active" || bad "totalray.service is not active"
  systemctl is-active --quiet sing-box && ok "sing-box.service is active" || bad "sing-box.service is not active"
  [ "$(cfg live_monitor.enabled true)" = "true" ] && ok "live_monitor.enabled" || bad "live_monitor is disabled in config"

  local age
  age="$(jq -r 'now - .last_check | floor' "$DATA_DIR/live_monitor_status.json" 2>/dev/null || echo 999999)"
  if [ "$age" -lt 30 ]; then ok "live monitor heartbeat is ${age}s old"; else bad "live monitor heartbeat is stale (${age}s)"; fi

  SHADOW="$(cfg live_monitor.shadow_mode true)"
  info "shadow_mode=$SHADOW  -> expecting a $( [ "$SHADOW" = true ] && echo 'logged recommendation and no switch' || echo 'real switch, refill and breaker')"

  ACTIVE="$(active_tag)" || die "Clash API not reachable at $CLASH"
  info "active outbound: $ACTIVE"
  local signal_tag
  signal_tag="$(lm '.passive_signal.active_tag')"
  if [ "$signal_tag" != "$ACTIVE" ]; then   # the status file is a few seconds old
    sleep 12
    ACTIVE="$(active_tag)"
    signal_tag="$(lm '.passive_signal.active_tag')"
  fi
  [ "$signal_tag" = "$ACTIVE" ] && ok "monitor watches the active outbound" || bad "monitor watches '$signal_tag', Clash says '$ACTIVE'"

  SERVER="$(server_of "$ACTIVE")"
  [ -n "$SERVER" ] || die "cannot find the server of $ACTIVE in $SINGBOX_CONF"
  mapfile -t SERVER_IPS < <(ips_of "$SERVER")
  [ "${#SERVER_IPS[@]}" -gt 0 ] || die "cannot resolve $SERVER"
  info "server: $SERVER -> ${SERVER_IPS[*]}"
  info "(if this is a CDN address, other configs behind it are impaired too)"

  local iface root
  iface="$(egress_if "${SERVER_IPS[0]}")"
  [ -n "$iface" ] && ok "egress interface: $iface" || bad "no route to ${SERVER_IPS[0]}"
  root="$(tc qdisc show dev "$iface" root 2>/dev/null | awk 'NR==1 {print $2}')"
  case "$root" in
    noqueue | fq_codel | pfifo_fast | mq | fq | netem | prio) ok "root qdisc on $iface is '$root' (netem can replace it)" ;;
    *) if [ "$FORCE" = 1 ]; then info "root qdisc '$root' will be replaced (FORCE=1)"; else bad "root qdisc '$root' looks custom; set FORCE=1 to let this test replace it"; fi ;;
  esac

  local ready
  ready="$(jq -r '[.standbys // {} | to_entries[] | select(.value.ready)] | length' "$DATA_DIR/live_monitor_status.json" 2>/dev/null || echo 0)"
  [ "$ready" -ge 1 ] && ok "$ready ready standby(s)" || bad "no ready standby; failover has nowhere to go"

  local pb need_pb
  pb="$(pool_b_count)"
  need_pb=$(( $(cfg live_monitor.circuit_breaker_failures 3) + 2 ))
  info "pool B has $pb verified config(s); the breaker test needs at least $need_pb"
  [ "$pb" != "?" ] && [ "$pb" -ge 3 ] && ok "pool B large enough for the switch test" || bad "pool B too small for the switch test"
  CAN_BREAKER=0
  if [ "$pb" != "?" ] && [ "$pb" -ge "$need_pb" ]; then CAN_BREAKER=1; fi
  [ "$(lm '.failover.circuit_open')" = "false" ] && ok "failover breaker is closed" || bad "failover breaker is already open"
  info "tunnel sockets seen by 'ss -tnp' (sing-box -> public IPs): $(tunnel_sockets)"
}

through_active() {  # connections whose chain includes the active tag
  clash_get /connections | jq -r --arg t "$1" '[.connections[] | select(any(.chains[]?; . == $t))] | length'
}

check_load_uses_active() {
  local n
  n="$(through_active "$ACTIVE" 2>/dev/null || echo 0)"
  if [ "$n" -ge 1 ]; then ok "traffic flows through the active outbound ($n connection(s))"
  else bad "no connection goes through $ACTIVE; the test cannot trigger the signal"; fi
}

switch_changed() {
  if [ "$SHADOW" = true ]; then
    # the history keeps only the last 50 events, so its length stops growing;
    # compare the timestamp of the newest event instead
    [ "$(lm '.failover.history[-1].at')" != "$BEFORE_AT" ]
  else
    [ "$(active_tag)" != "$BEFORE_TAG" ]
  fi
}

scenario_switch() {
  step "switch: degrade the active server"
  setup_clash
  BEFORE_TAG="$(active_tag)"
  ACTIVE="$BEFORE_TAG"
  BEFORE_COUNT="$(lm '.failover_count')"
  BEFORE_AT="$(lm '.failover.history[-1].at')"
  local server
  server="$(server_of "$ACTIVE")"
  mapfile -t SERVER_IPS < <(ips_of "$server")
  T_START=$SECONDS

  start_load
  sleep 5
  check_load_uses_active

  step "baseline: ${BASELINE_SECONDS}s of load with no impairment must NOT look degraded"
  local t0=$SECONDS false_positive=0
  while [ $((SECONDS - t0)) -lt "$BASELINE_SECONDS" ]; do
    [ "$(lm '.passive_signal.degraded')" = "true" ] && false_positive=1
    sleep 2
  done
  line_now
  [ "$false_positive" -eq 0 ] && ok "no false degradation under normal load" || bad "signal went 'degraded' with no impairment (false positive)"
  [ "$(active_tag)" = "$BEFORE_TAG" ] && ok "active outbound unchanged during baseline" || bad "active outbound changed during baseline"

  step "impair $ACTIVE and wait up to ${WAIT_SWITCH}s"
  T_START=$SECONDS
  impair "${SERVER_IPS[@]}"
  local detected=0
  wait_until "$WAIT_SWITCH" switch_changed && detected=1
  local latency=$((SECONDS - T_START))
  line_now
  clear_impair
  stop_load

  if [ "$SHADOW" = true ]; then
    [ "$detected" -eq 1 ] && ok "recommendation recorded after ${latency}s" || bad "no recommendation within ${WAIT_SWITCH}s"
    [ "$(lm '.failover.history[-1].shadow')" = "true" ] && ok "history entry is marked shadow" || bad "last history entry is not a shadow entry"
    [ "$(active_tag)" = "$BEFORE_TAG" ] && ok "shadow mode did NOT switch" || bad "active outbound changed in shadow mode"
    return
  fi

  [ "$detected" -eq 1 ] && ok "switched $BEFORE_TAG -> $(active_tag) after ${latency}s" || { bad "no switch within ${WAIT_SWITCH}s"; return; }
  [ "$(lm '.failover_count')" -eq $((BEFORE_COUNT + 1)) ] && ok "failover_count incremented once" || bad "failover_count is $(lm '.failover_count'), expected $((BEFORE_COUNT + 1))"
  [ "$(lm '.failover.history[-1].ok')" = "true" ] && ok "history records a successful switch" || bad "history does not record a successful switch"
  [ "$(lm '.failover.last_target')" = "$(active_tag)" ] && ok "controller remembers the promoted server" || bad "last_target does not match the active outbound"
  [ "$(lm '.failover.failures_in_window')" = "0" ] && ok "the first switch is not counted as a failure" || bad "failures_in_window=$(lm '.failover.failures_in_window') right after the first switch"

  step "refill: the vacated slot is filled again (up to ${WAIT_REFILL}s)"
  local want have
  want="$(cfg proxy_group.standby_count 3)"
  refilled() { have="$(group_tags | wc -l | tr -d ' ')"; [ "$have" -ge "$want" ]; }
  if wait_until "$WAIT_REFILL" refilled; then ok "group is back to $have member(s) (wanted $want)"
  else bad "group has $have member(s), wanted $want"; fi
  if group_tags | grep -qx "$BEFORE_TAG"; then
    info "note: $BEFORE_TAG is back in the group (a Pool B round re-added it after the impairment ended)"
  else
    ok "$BEFORE_TAG left the group"
  fi
}

scenario_breaker() {
  step "breaker: repeated promoted failures open the circuit breaker"
  setup_clash
  if [ "$(cfg live_monitor.shadow_mode true)" = true ]; then
    info "skipped: shadow_mode is on (the breaker only acts in live mode)"
    return
  fi
  local max cooldown round tag server breaker_round=0
  max="$(cfg live_monitor.circuit_breaker_failures 3)"
  cooldown="$(cfg live_monitor.cooldown_seconds 60)"
  [ "${CAN_BREAKER:-1}" -eq 1 ] || info "warning: pool B may be too small to complete this test"
  info "expected: switches in rounds 1..$max, breaker opens in round $((max + 1)) (no further switch)"
  start_load
  sleep 5
  for round in $(seq 1 $((max + 1))); do
    tag="$(active_tag)"
    server="$(server_of "$tag")"
    mapfile -t SERVER_IPS < <(ips_of "$server")
    T_START=$SECONDS
    info "round $round: impairing $tag"
    impair "${SERVER_IPS[@]}"
    breaker_or_switch() { [ "$(lm '.failover.circuit_open')" = "true" ] || [ "$(active_tag)" != "$tag" ]; }
    wait_until $((cooldown + WAIT_SWITCH)) breaker_or_switch || true
    line_now
    clear_impair
    if [ "$(lm '.failover.circuit_open')" = "true" ]; then breaker_round=$round; break; fi
    [ "$(active_tag)" != "$tag" ] || { bad "round $round: no switch and no breaker"; break; }
    info "switched $tag -> $(active_tag); waiting ${cooldown}s for the cooldown"
    sleep $((cooldown + 5))
  done

  if [ "$breaker_round" -eq $((max + 1)) ]; then ok "breaker opened in round $breaker_round, as expected"
  elif [ "$breaker_round" -gt 0 ]; then bad "breaker opened in round $breaker_round, expected $((max + 1)) (counting too early?)"
  else bad "breaker never opened"; fi
  [ "$(lm '.failover.failures_in_window')" = "$max" ] && ok "failures_in_window = $max" || bad "failures_in_window = $(lm '.failover.failures_in_window'), expected $max"

  if [ "$breaker_round" -gt 0 ]; then
    step "no switch while the breaker is open"
    tag="$(active_tag)"
    server="$(server_of "$tag")"
    mapfile -t SERVER_IPS < <(ips_of "$server")
    impair "${SERVER_IPS[@]}"
    sleep 30
    line_now
    clear_impair
    [ "$(active_tag)" = "$tag" ] && ok "active outbound held while the breaker was open" || bad "switched while the breaker was open"
    local window
    window="$(cfg live_monitor.circuit_breaker_window_seconds 300)"
    info "the breaker closes by itself ${window}s after the last failure (check 'totalray status' then)"
  fi
  stop_load
}

summary() {
  step "journal (failover lines since the test started)"
  journalctl -u totalray --since "-$((SECONDS - T_START + 600))s" --no-pager 2>/dev/null |
    grep -Ei "failover|shadow recommendation|circuit" | tail -n 15 || info "(none)"
  printf '\n== result: %d passed, %d failed\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ]
}

main() {
  local cmd="${1:-help}"
  case "$cmd" in
    plan)  # plan IFACE IP...   (print the tc commands, change nothing)
      shift
      plan_commands "${1:?iface}" "$NETEM_PROFILE" "${@:2}"
      return
      ;;
    clear)
      [ "$(id -u)" -eq 0 ] || die "run as root"
      local iface found=0
      for iface in $(ls /sys/class/net); do
        # only touch interfaces that actually carry a netem rule from this test
        if tc qdisc show dev "$iface" 2>/dev/null | grep -q netem; then
          tc qdisc del dev "$iface" root 2>/dev/null || true
          echo "netem removed from $iface (root qdisc is back to the default)"
          found=1
        fi
      done
      [ "$found" -eq 1 ] || echo "no netem rules found"
      return
      ;;
    help | -h | --help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      return
      ;;
    preflight | switch | breaker | all) ;;
    *) die "unknown command '$cmd' (try: help)" ;;
  esac

  [ "$(id -u)" -eq 0 ] || die "run as root"
  trap cleanup EXIT INT TERM
  preflight
  case "$cmd" in
    switch)  scenario_switch ;;
    breaker) scenario_breaker ;;
    all)     scenario_switch; scenario_breaker ;;
  esac
  summary
}

main "$@"
