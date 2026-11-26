#!/usr/bin/env bash
# Copy the latest CampusPi Python code from this directory to /opt/campuspi
# WITHOUT apt / internet access. Existing /etc/campuspi/config.json is kept.
#
# Usage:
#   sudo bash deploy_code_only.sh
#
# This is the offline update path. For a full reinstall (installs hostapd,
# dnsmasq, etc.) use install.sh or deploy_all.sh instead.

set -eu

if [ "$(id -u)" -ne 0 ]; then
  exec sudo bash "$0" "$@"
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/opt/campuspi"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "==> Source: $HERE"
echo "==> Target: $APP"

if [ -d "$APP" ]; then
  BAK="$APP.bak.$STAMP"
  if cp -a "$APP" "$BAK" 2>/dev/null; then
    echo "==> Backed up old code to $BAK"
  else
    echo "WARNING: could not back up $APP (disk full?)" >&2
  fi
fi

install -d "$APP/web"
install -m 0644 \
  "$HERE/main.py" "$HERE/config.py" "$HERE/utils.py" \
  "$HERE/portal.py" "$HERE/network.py" "$HERE/hotspot.py" \
  "$HERE/webui.py" "$HERE/nla.py" "$HERE/selftest.py" \
  "$APP/"
install -m 0644 "$HERE/web/index.html" "$APP/web/"
install -m 0755 \
  "$HERE/setup_rdp.sh" "$HERE/setup_tuning.sh" "$HERE/repair_desktop.sh" \
  "$APP/"
install -m 0644 "$HERE/campuspi.service" /etc/systemd/system/campuspi.service

rm -rf "$APP/__pycache__"
systemctl daemon-reload
systemctl enable campuspi
systemctl restart campuspi || systemctl start campuspi
sleep 2
systemctl is-active campuspi || true

echo ""
echo "==> Code updated. If the network is still broken, run:"
echo "    sudo bash $HERE/campuspi-recover.sh"
echo "    sudo reboot"
