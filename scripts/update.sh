#!/usr/bin/env bash
# Update an installed TotalRay instance from the latest origin/main.
# Run: sudo /usr/local/sbin/totalray-update
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }

REPO_URL="${TOTALRAY_REPO_URL:-https://github.com/GrayHatAli/TotalRay.git}"
REPO_BASE="${REPO_URL%.git}"
INSTALL_DIR="/opt/totalray"
SERVICE="totalray.service"
TMP_DIR="$(mktemp -d -t totalray-update.XXXXXX)"
SERVICE_WAS_ACTIVE=0

cleanup() {
  rc=$?
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ] && ! systemctl is-active --quiet "$SERVICE"; then
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
  exit "$rc"
}
trap cleanup EXIT

echo "== Downloading latest TotalRay from main..."
curl -fsSL --retry 3 \
  "${REPO_BASE%/}/archive/refs/heads/main.tar.gz" \
  -o "$TMP_DIR/totalray.tar.gz"
tar -xzf "$TMP_DIR/totalray.tar.gz" -C "$TMP_DIR"
SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'TotalRay-*' -print -quit)"
[ -n "$SRC_DIR" ] || { echo "Could not unpack the TotalRay source." >&2; exit 1; }

[ -d "$INSTALL_DIR/venv" ] || {
  echo "Missing $INSTALL_DIR/venv; run scripts/install.sh first." >&2
  exit 1
}

echo "== Stopping TotalRay..."
if systemctl is-active --quiet "$SERVICE"; then
  SERVICE_WAS_ACTIVE=1
  systemctl stop "$SERVICE"
fi

echo "== Installing application files..."
rm -rf "$INSTALL_DIR/totalray"
cp -a "$SRC_DIR/totalray" "$INSTALL_DIR/totalray"
cp "$SRC_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

# Keep the existing configuration, database, rule-sets, and sing-box files.
echo "== Updating Python dependencies..."
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

install -m 0644 "$SRC_DIR/systemd/totalray.service" \
  /etc/systemd/system/totalray.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

echo "== Starting TotalRay..."
systemctl start "$SERVICE"
systemctl --no-pager --full status "$SERVICE" | sed -n '1,12p'
echo "Update complete. Configuration and runtime data were preserved."
