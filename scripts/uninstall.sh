#!/usr/bin/env bash
# Uninstall pi-vpn-gateway - database and configs are kept unless --purge is given
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo."; exit 1; }

systemctl disable --now vpnman 2>/dev/null || true
systemctl disable --now sing-box 2>/dev/null || true
rm -f /etc/systemd/system/vpnman.service /usr/local/bin/vpnman
rm -f /etc/dnsmasq.d/pi-gateway.conf /etc/sysctl.d/99-pivpn.conf
systemctl daemon-reload
systemctl restart dnsmasq 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf /opt/vpnman /etc/vpnman /var/lib/vpnman /etc/sing-box
  echo "Fully removed (including data)."
else
  rm -rf /opt/vpnman
  echo "Removed. Data under /etc/vpnman and /var/lib/vpnman was kept."
fi
echo "Note: if you had turned off the router's DHCP, turn it back on."
