#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Install pi-vpn-gateway on Raspberry Pi OS (bookworm or newer, 64/32-bit)
#  Run:  sudo bash scripts/install.sh
# ══════════════════════════════════════════════════════════════
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo."; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FALLBACK_VER="1.13.14"

echo "== [1/7] Installing prerequisites..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl dnsmasq python3 python3-venv python3-pip sqlite3 nftables jq ca-certificates

echo "== [2/7] Installing sing-box..."
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

echo "== [3/7] Copying files and building the Python environment..."
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

echo "== [4/7] Detecting the network and setting the local DNS..."
DEF_IFACE="$(ip route show default | awk '/default/{print $5; exit}')"
GW="$(ip route show default | awk '/default/{print $3; exit}')"
PI_IP="$(ip -4 addr show dev "$DEF_IFACE" | awk '/inet /{print $2; exit}' | cut -d/ -f1)"
echo "   interface: $DEF_IFACE | Pi IP: $PI_IP | router: $GW"
[ -n "$GW" ] && sed -i "s|^  local_server:.*|  local_server: \"$GW\"|" /etc/totalray/config.yaml

echo "== [5/7] Enabling IP forwarding and dnsmasq..."
cat > /etc/sysctl.d/99-pivpn.conf <<'EOF2'
net.ipv4.ip_forward=1
EOF2
sysctl -q --system >/dev/null || true

SUBNET="${PI_IP%.*}"
sed -e "s|__PI_IP__|$PI_IP|g" \
    -e "s|__RANGE_START__|${SUBNET}.100|g" \
    -e "s|__RANGE_END__|${SUBNET}.200|g" \
    "$SRC_DIR/dnsmasq/pi-gateway.conf" > /etc/dnsmasq.d/pi-gateway.conf
# The Pi's own resolver goes straight to the router, not dnsmasq (avoids a loop)
if [ -n "$GW" ]; then
  chattr -i /etc/resolv.conf 2>/dev/null || true
  printf 'nameserver %s\n' "$GW" > /etc/resolv.conf
fi
systemctl enable --now dnsmasq >/dev/null 2>&1 || true
systemctl restart dnsmasq

echo "== [6/7] Installing services..."
cp "$SRC_DIR/systemd/totalray.service" /etc/systemd/system/totalray.service
systemctl daemon-reload
systemctl enable sing-box totalray >/dev/null 2>&1 || true

echo "== [7/7] Downloading the Iran rule-sets..."
totalray update-rules || true

cat <<DONE

╔══════════════════════════════════════════════════════════════╗
║  Install complete                                             ║
╚══════════════════════════════════════════════════════════════╝

Next steps:
  1) Add your subscriptions to the end of /etc/totalray/config.yaml:
       subscriptions:
         - "https://..."
  2) Or with:   sudo totalray add-sub "https://..."
  3) Start the service:   sudo systemctl start totalray
  4) Check status:        sudo totalray status

Network (important - pick one):
  a) Turn off the router's DHCP so dnsmasq on the Pi ($PI_IP)
     hands out addresses and every device is routed through this
     gateway automatically, or
  b) Keep the router's DHCP, but manually set each device's
     gateway/DNS to $PI_IP.

Logs:  journalctl -u totalray -f   |   journalctl -u sing-box -f
DONE
