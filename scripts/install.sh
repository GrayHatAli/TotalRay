#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Install TotalRay on any Debian-based Linux box (Raspberry Pi,
#  an old laptop turned home server, or a cloud VPS) - bookworm or
#  newer, 64/32-bit.
#  Run:  sudo bash scripts/install.sh
#
#  Two deployment modes - pick with --mode or $TOTALRAY_MODE:
#    gateway  Becomes the LAN's transparent default gateway (the
#             classic "Raspberry Pi sitting between the router and
#             the network" setup). Devices need zero configuration.
#    proxy    No LAN to sit in front of - e.g. a cloud VPS. Runs
#             the same subscription-testing/pool engine, but
#             clients connect in from the internet to a single
#             stable SOCKS5/HTTP endpoint (`lan_proxy` in
#             config.yaml) instead of installing/rotating their
#             own v2ray-style client configs.
# ══════════════════════════════════════════════════════════════
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo."; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FALLBACK_VER="1.13.14"

# ---- mode selection -------------------------------------------------
MODE="${TOTALRAY_MODE:-}"
for arg in "$@"; do
  case "$arg" in
    --mode=*) MODE="${arg#--mode=}" ;;
  esac
done
if [ -z "$MODE" ] && [ -t 0 ]; then
  echo "Deploy TotalRay as:"
  echo "  1) LAN gateway  - this box becomes the network's default gateway"
  echo "                    (Raspberry Pi / home server on your own LAN)"
  echo "  2) Remote proxy - no LAN here (e.g. a cloud VPS); devices connect"
  echo "                    in over the internet to one stable proxy endpoint"
  read -rp "Choice [1/2] (default 1): " CHOICE
  case "$CHOICE" in
    2) MODE="proxy" ;;
    *) MODE="gateway" ;;
  esac
fi
MODE="${MODE:-gateway}"
case "$MODE" in
  gateway|proxy) ;;
  *) echo "Unknown --mode '$MODE' (expected 'gateway' or 'proxy')"; exit 1 ;;
esac
echo "== Mode: $MODE"

echo "== [1/8] Installing prerequisites..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS="curl python3 python3-venv python3-pip sqlite3 nftables jq ca-certificates"
[ "$MODE" = "gateway" ] && PKGS="$PKGS dnsmasq"
apt-get install -y -qq $PKGS

echo "== [2/8] Installing sing-box..."
ARCH="$(dpkg --print-architecture)"   # arm64 | armhf | amd64
case "$ARCH" in arm64|armhf|amd64) ;; *) echo "Unsupported architecture: $ARCH"; exit 1;; esac

VER="$(curl -fsSL --max-time 20 https://api.github.com/repos/SagerNet/sing-box/releases/latest 2>/dev/null | jq -r '.tag_name // empty' | tr -d 'v' || true)"
[ -n "$VER" ] || VER="$FALLBACK_VER"
echo "   version: $VER - arch: $ARCH"

if ! command -v sing-box >/dev/null 2>&1; then
  DEB_URL="https://github.com/SagerNet/sing-box/releases/download/v${VER}/sing-box_${VER}_linux_${ARCH}.deb"
  curl -fSL --retry 3 -o /tmp/sing-box.deb "$DEB_URL" || \
  curl -fSL --retry 3 -o /tmp/sing-box.deb "https://gh-proxy.com/${DEB_URL}" || true
  if [ -s /tmp/sing-box.deb ]; then
    dpkg -i /tmp/sing-box.deb || apt-get -f install -y
    rm -f /tmp/sing-box.deb
  fi
fi
command -v sing-box >/dev/null 2>&1 || { echo "sing-box install failed; install it manually and re-run."; exit 1; }
sing-box version

echo "== [3/8] Copying files and building the Python environment..."
install -d /opt/totalray /etc/totalray /var/lib/totalray /etc/sing-box/rules
rm -rf /opt/totalray/totalray
cp -r "$SRC_DIR/totalray" /opt/totalray/totalray
cp "$SRC_DIR/requirements.txt" /opt/totalray/requirements.txt
python3 -m venv /opt/totalray/venv
/opt/totalray/venv/bin/pip install -q --upgrade pip
/opt/totalray/venv/bin/pip install -q -r /opt/totalray/requirements.txt

cat > /usr/local/bin/totalray <<'WRAPEOF'
#!/usr/bin/env bash
# cd into /opt/totalray first: `python -m totalray` resolves the package
# relative to the current working directory, not to this script's path.
# Without this, the command fails with "No module named totalray" whenever
# it's invoked from any other directory.
cd /opt/totalray && exec /opt/totalray/venv/bin/python -m totalray --config /etc/totalray/config.yaml "$@"
WRAPEOF
chmod +x /usr/local/bin/totalray

[ -f /etc/totalray/config.yaml ] || cp "$SRC_DIR/config.yaml" /etc/totalray/config.yaml

echo "== [4/8] Detecting the network..."
DEF_IFACE="$(ip route show default | awk '/default/{print $5; exit}')"
GW="$(ip route show default | awk '/default/{print $3; exit}')"
BOX_IP="$(ip -4 addr show dev "$DEF_IFACE" | awk '/inet /{print $2; exit}' | cut -d/ -f1)"
echo "   interface: $DEF_IFACE | box IP: $BOX_IP | default gateway: $GW"

if [ "$MODE" = "gateway" ]; then
  # This box IS the LAN's gateway/DNS from the clients' point of view, so
  # its own "local_server" (used for Iran-direct DNS + direct-first
  # fetching) is the *router* upstream of it.
  [ -n "$GW" ] && sed -i "s|^  local_server:.*|  local_server: \"$GW\"|" /etc/totalray/config.yaml
else
  # No LAN here - keep whatever resolver this box already uses (its own
  # /etc/resolv.conf), rather than pointing at a cloud provider's gateway
  # IP, which usually isn't a DNS server at all.
  UPSTREAM_DNS="$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null || true)"
  [ -n "$UPSTREAM_DNS" ] && sed -i "s|^  local_server:.*|  local_server: \"$UPSTREAM_DNS\"|" /etc/totalray/config.yaml
fi

echo "== [5/8] Generating a random proxy password..."
if grep -q '^  password: "CHANGE_ME"' /etc/totalray/config.yaml; then
  RANDPASS="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
  sed -i "s|^  password: \"CHANGE_ME\"|  password: \"$RANDPASS\"|" /etc/totalray/config.yaml
fi

if [ "$MODE" = "gateway" ]; then
  echo "== [6/8] Enabling IP forwarding and dnsmasq..."
  cat > /etc/sysctl.d/99-totalray.conf <<'EOF2'
net.ipv4.ip_forward=1
EOF2
  sysctl -q --system >/dev/null || true

  SUBNET="${BOX_IP%.*}"
  sed -e "s|__PI_IP__|$BOX_IP|g" \
      -e "s|__RANGE_START__|${SUBNET}.100|g" \
      -e "s|__RANGE_END__|${SUBNET}.200|g" \
      "$SRC_DIR/dnsmasq/pi-gateway.conf" > /etc/dnsmasq.d/pi-gateway.conf
  # This box's own resolver goes straight to the router, not dnsmasq (avoids a loop)
  if [ -n "$GW" ]; then
    chattr -i /etc/resolv.conf 2>/dev/null || true
    printf 'nameserver %s\n' "$GW" > /etc/resolv.conf
  fi
  systemctl enable --now dnsmasq >/dev/null 2>&1 || true
  systemctl restart dnsmasq
else
  echo "== [6/8] Proxy mode: skipping DHCP/dnsmasq (no LAN to serve)."
fi

echo "== [7/8] Installing services..."
install -m 0644 "$SRC_DIR/systemd/totalray.service" /etc/systemd/system/totalray.service
systemctl daemon-reload
systemctl enable sing-box
systemctl enable totalray
systemctl cat totalray >/dev/null

echo "== [8/8] Downloading the Iran rule-sets..."
totalray update-rules || true

PROXY_PORT="$(awk '/^lan_proxy:/{f=1} f && /port:/{print $2; exit}' /etc/totalray/config.yaml)"
PROXY_USER="$(awk '/^lan_proxy:/{f=1} f && /username:/{gsub(/"/,"",$2); print $2; exit}' /etc/totalray/config.yaml)"
PROXY_PASS="$(awk '/^lan_proxy:/{f=1} f && /password:/{gsub(/"/,"",$2); print $2; exit}' /etc/totalray/config.yaml)"

cat <<DONE

╔══════════════════════════════════════════════════════════════╗
║  Install complete  (mode: $MODE)
╚══════════════════════════════════════════════════════════════╝

Next steps:
  1) Add your subscriptions to the end of /etc/totalray/config.yaml:
       subscriptions:
         - "https://..."
  2) Or with:   sudo totalray add-sub "https://..."
  3) Start the service:   sudo systemctl start totalray
  4) Check status:        sudo totalray status

DONE

if [ "$MODE" = "gateway" ]; then
cat <<DONE
Network (important - pick one):
  a) Turn off the router's DHCP so dnsmasq on this box ($BOX_IP)
     hands out addresses and every device is routed through this
     gateway automatically, or
  b) Keep the router's DHCP, but change its "Default Gateway" field
     (not the router's own LAN IP) to $BOX_IP.
DONE
else
PUB_IP="$(curl -fsSL --max-time 5 https://api.ipify.org || echo "$BOX_IP")"
cat <<DONE
Remote proxy mode - point your devices at this server instead of
installing v2ray-style client configs:

  Server:    $PUB_IP
  Port:      ${PROXY_PORT:-2081}   (SOCKS5 and HTTP on the same port)
  Username:  ${PROXY_USER:-totalray}
  Password:  ${PROXY_PASS:-<see lan_proxy.password in /etc/totalray/config.yaml>}

Set this as the system (or per-app) SOCKS5/HTTP proxy on each device.
Reminders:
  - Open/allow port ${PROXY_PORT:-2081} in this VPS's cloud firewall /
    security group (this script only configures the box itself).
  - The proxy always exits through whichever server is currently in
    pool B (verified) - devices never need to touch a config again.
DONE
fi

cat <<DONE

Logs:  journalctl -u totalray -f   |   journalctl -u sing-box -f
DONE
