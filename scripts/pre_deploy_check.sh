#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Pre-deploy health check for TotalRay upgrades.
#  Run BEFORE and AFTER deploying to verify the Pi is healthy.
#
#  Usage:  sudo bash scripts/pre_deploy_check.sh [--post]
#
#  Exit code 0 = healthy, 1 = problems detected.
# ══════════════════════════════════════════════════════════════
set -euo pipefail

POST_DEPLOY=0
for arg in "$@"; do
  case "$arg" in
    --post) POST_DEPLOY=1 ;;
  esac
done

PASS=0
FAIL=0
WARN=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ✓ $label"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $label"
    FAIL=$((FAIL + 1))
  fi
}

warn_check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ✓ $label"
    PASS=$((PASS + 1))
  else
    echo "  ⚠ $label"
    WARN=$((WARN + 1))
  fi
}

if [ "$POST_DEPLOY" -eq 1 ]; then
  echo "═══ Post-deploy health check ═══"
else
  echo "═══ Pre-deploy health check ═══"
fi
echo

# -- sing-box binary --
echo "sing-box:"
check "binary exists" command -v sing-box
if command -v sing-box >/dev/null 2>&1; then
  check "config valid" sing-box check -c /etc/sing-box/config.json
fi

# -- sing-box service --
echo "sing-box service:"
check "service running" systemctl is-active sing-box
check "no crash-loop" bash -c '! systemctl show sing-box --property=NRestarts --value | grep -qE "^[1-9]"'

# -- totalray service --
echo "totalray service:"
check "service running" systemctl is-active totalray

# -- Clash API --
echo "Clash API:"
CLASH_SECRET="$(awk '/^  secret:/{gsub(/\"/,"",$2); print $2}' /etc/totalray/config.yaml 2>/dev/null || true)"
CLASH_AUTH=""
[ -n "$CLASH_SECRET" ] && CLASH_AUTH="-H 'Authorization: Bearer $CLASH_SECRET'"
check "API reachable" bash -c "curl -fsS --max-time 3 http://127.0.0.1:9090/version $CLASH_AUTH"

# -- database --
echo "Database:"
check "SQLite readable" sqlite3 /var/lib/totalray/totalray.db "SELECT COUNT(*) FROM configs;"
POOL_B_COUNT="$(sqlite3 /var/lib/totalray/totalray.db "SELECT COUNT(*) FROM configs WHERE removed=0 AND pool='b';" 2>/dev/null || echo 0)"
if [ "$POOL_B_COUNT" -eq 0 ]; then
  echo "  ⚠ Pool B is empty (0 verified configs)"
  WARN=$((WARN + 1))
else
  echo "  ✓ Pool B has $POOL_B_COUNT verified configs"
  PASS=$((PASS + 1))
fi

# -- round status --
echo "Round status:"
RS_PATH="/var/lib/totalray/round_status.json"
if [ -f "$RS_PATH" ]; then
  check "valid JSON" python3 -c "import json; json.load(open('$RS_PATH'))"
  # Check for stale running rounds
  STALE_RUNNING="$(python3 -c "
import json
d = json.load(open('$RS_PATH'))
running = [k for k,v in d.items() if isinstance(v, dict) and v.get('running')]
print(' '.join(running) if running else '')
" 2>/dev/null || true)"
  if [ -n "$STALE_RUNNING" ]; then
    echo "  ⚠ Stale running rounds detected: $STALE_RUNNING"
    WARN=$((WARN + 1))
  fi
else
  echo "  ⚠ No round_status.json found"
  WARN=$((WARN + 1))
fi

# -- apply state --
echo "Apply state:"
AS_PATH="/var/lib/totalray/apply_state.json"
if [ -f "$AS_PATH" ]; then
  CIRCUIT_OPEN="$(python3 -c "import json; print(json.load(open('$AS_PATH')).get('circuit_open', False))" 2>/dev/null || echo False)"
  if [ "$CIRCUIT_OPEN" = "True" ]; then
    echo "  ⚠ Circuit breaker is OPEN"
    WARN=$((WARN + 1))
  else
    echo "  ✓ Circuit breaker closed"
    PASS=$((PASS + 1))
  fi
else
  echo "  - No apply state yet (first run)"
fi

# -- disk space --
echo "Disk:"
DISK_FREE="$(df -BM /var/lib/totalray | awk 'NR==2{print $4}' | tr -d 'M')"
if [ "${DISK_FREE:-0}" -lt 100 ]; then
  echo "  ⚠ Low disk space: ${DISK_FREE}MB free"
  WARN=$((WARN + 1))
else
  echo "  ✓ ${DISK_FREE}MB free"
  PASS=$((PASS + 1))
fi

# -- journal errors (last 5 min) --
echo "Recent journal errors:"
RECENT_ERRORS="$(journalctl -u totalray --since "5 min ago" -p err --no-pager -q 2>/dev/null | wc -l)"
if [ "$RECENT_ERRORS" -gt 0 ]; then
  echo "  ⚠ $RECENT_ERRORS error(s) in the last 5 minutes"
  WARN=$((WARN + 1))
  journalctl -u totalray --since "5 min ago" -p err --no-pager -q 2>/dev/null | tail -3 | sed 's/^/    /'
else
  echo "  ✓ No errors in the last 5 minutes"
  PASS=$((PASS + 1))
fi

echo
echo "═══ Results: $PASS passed, $FAIL failed, $WARN warnings ═══"

if [ "$FAIL" -gt 0 ]; then
  echo "❌ Deploy should NOT proceed. Fix the failures above."
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo "⚠️  Deploy can proceed, but review warnings."
  exit 0
else
  echo "✅ All checks passed."
  exit 0
fi
