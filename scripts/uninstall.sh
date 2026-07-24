#!/usr/bin/env bash
# Uninstall pi-vpn-gateway - database and configs are kept unless --purge is given
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo."; exit 1; }

systemctl disable --now totalray 2>/dev/null || true
systemctl disable --now sing-box 2>/dev/null || true
rm -f /etc/systemd/system/totalray.service /usr/local/bin/totalray
rm -f /etc/dnsmasq.d/pi-gateway.conf /etc/sysctl.d/99-pivpn.conf
systemctl daemon-reload
systemctl restart dnsmasq 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf /opt/totalray /etc/totalray /var/lib/totalray /etc/sing-box
  echo "Fully removed (including data)."
else
  rm -rf /opt/totalray
  echo "Removed. Data under /etc/totalray and /var/lib/totalray was kept."
fi
echo "Note: if you had turned off the router's DHCP, turn it back on."
