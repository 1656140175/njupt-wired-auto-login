#!/usr/bin/env bash
# CampusPi recovery script for Raspberry Pi OS (Bullseye / Bookworm).
#
# Copy this file to the root of the Pi's boot partition (the small FAT32
# volume), boot the Pi, then run:
#
#   sudo bash /boot/campuspi-recover.sh
#
# On Bookworm the boot partition is /boot/firmware, so use:
#   sudo bash /boot/firmware/campuspi-recover.sh
#
# The script:
#   - backs up every file it changes (*.bak.YYYYmmdd-HHMMSS)
#   - finds the campus / PC / Wi-Fi interfaces
#   - restores 192.168.9.1/24 on the PC Ethernet and 192.168.10.1/24 on Wi-Fi
#   - writes a known-good dnsmasq config (no option 3 on the PC side)
#   - rewrites hostapd.conf from /etc/campuspi/config.json
#   - restarts dnsmasq, hostapd and campuspi
#   - writes a log to the boot partition for easy retrieval from Windows
#
# It does NOT change the campus username/password.

set -u

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

BOOT_DIR="/boot"
if [ -d /boot/firmware ]; then
  BOOT_DIR="/boot/firmware"
fi
LOG="$BOOT_DIR/campuspi-recover.log"
if ! touch "$LOG" 2>/dev/null; then
  LOG="/tmp/campuspi-recover.log"
fi
exec > >(tee -a "$LOG") 2>&1

TS="$(date +%Y%m%d-%H%M%S)"
CFG="/etc/campuspi/config.json"
HOSTAPD_CONF="/etc/hostapd/hostapd.conf"
DNSMASQ_CONF="/etc/dnsmasq.d/campuspi.conf"

say() { printf '\n===== %s =====\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*"; }
backup() {
  local f="$1"
  if [ -e "$f" ]; then
    cp -a "$f" "$f.bak.$TS" && echo "backup: $f -> $f.bak.$TS"
  fi
}
cfg_get() {
  python3 - "$CFG" "$1" <<'PY' 2>/dev/null
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(0)
cur = data
for part in key.split("."):
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break
if cur is None:
    cur = ""
print(cur)
PY
}
has_campus_ip() {
  ip -4 -o addr show dev "$1" 2>/dev/null | grep -q 'inet 10\.'
}
carrier_up() {
  [ "$(cat "/sys/class/net/$1/carrier" 2>/dev/null)" = "1" ]
}

say "CampusPi recovery $TS"
echo "host: $(hostname)"
uname -a
echo "boot dir: $BOOT_DIR"
echo "log: $LOG"

# Read-only root filesystem is a common cause of campuspi failing to write
# its dnsmasq/hostapd configs. Try a remount, but continue either way.
mount -o remount,rw / 2>/dev/null || true

say "Power / thermal"
if command -v vcgencmd >/dev/null 2>&1; then
  vcgencmd get_throttled 2>/dev/null || true
fi
dmesg 2>/dev/null | tail -n 40 || true

say "Disk"
df -h / /boot "$BOOT_DIR" 2>/dev/null || df -h /

say "Config"
CONFIG_OK=0
if [ -r "$CFG" ]; then
  if python3 - "$CFG" <<'PY'
import json, sys
json.load(open(sys.argv[1], encoding="utf-8"))
PY
  then
    CONFIG_OK=1
    echo "config JSON OK: $CFG"
  else
    warn "config JSON is invalid: $CFG"
    backup "$CFG"
  fi
else
  warn "config missing: $CFG"
fi

CAMPUS_IF="$(cfg_get campus.iface)"
[ -n "$CAMPUS_IF" ] || CAMPUS_IF="eth0"
WLAN_IF="$(cfg_get hotspot.iface)"
[ -n "$WLAN_IF" ] || WLAN_IF="wlan0"
SSID="$(cfg_get hotspot.ssid)"
[ -n "$SSID" ] || SSID="CampusPi"
HOTSPOT_PASS="$(cfg_get hotspot.password)"
CHANNEL="$(cfg_get hotspot.channel)"
[ -n "$CHANNEL" ] || CHANNEL="6"
[ "$CHANNEL" = "0" ] && CHANNEL="6"

say "Interfaces before"
ip -br link 2>/dev/null || true
ip -br addr 2>/dev/null || true
lsusb 2>/dev/null || true
rfkill list 2>/dev/null || true

# If an interface currently has a campus 10.x address, trust that over the
# config. This avoids serving DHCP on the campus-facing port if cables were
# swapped.
for n in $(ls /sys/class/net 2>/dev/null); do
  [ "$n" = "lo" ] && continue
  if has_campus_ip "$n"; then
    CAMPUS_IF="$n"
    break
  fi
done
if ! ip link show "$CAMPUS_IF" >/dev/null 2>&1; then
  warn "campus interface $CAMPUS_IF does not exist"
fi

if ! ip link show "$WLAN_IF" >/dev/null 2>&1; then
  for n in $(ls /sys/class/net 2>/dev/null); do
    if [ -d "/sys/class/net/$n/wireless" ]; then
      WLAN_IF="$n"
      break
    fi
  done
fi
if ! ip link show "$WLAN_IF" >/dev/null 2>&1; then
  warn "no wireless interface found (wlan0 missing)"
  WLAN_IF=""
fi

PC_IF=""
for n in eth1 eth2 usb0 usb1; do
  [ "$n" = "$CAMPUS_IF" ] && continue
  ip link show "$n" >/dev/null 2>&1 || continue
  has_campus_ip "$n" && continue
  carrier_up "$n" || continue
  PC_IF="$n"
  break
done
if [ -z "$PC_IF" ]; then
  for n in $(ls /sys/class/net 2>/dev/null | sort); do
    [ "$n" = "lo" ] && continue
    [ "$n" = "$CAMPUS_IF" ] && continue
    [ -d "/sys/class/net/$n/wireless" ] && continue
    [ -d "/sys/class/net/$n/device" ] || continue
    has_campus_ip "$n" && continue
    carrier_up "$n" || continue
    PC_IF="$n"
    break
  done
fi
# If nothing has carrier (PC unplugged), still choose a likely interface so
# that plugging the cable in later works without rerunning this script.
if [ -z "$PC_IF" ]; then
  for n in eth1 eth2 usb0 usb1; do
    [ "$n" = "$CAMPUS_IF" ] && continue
    ip link show "$n" >/dev/null 2>&1 || continue
    has_campus_ip "$n" && continue
    PC_IF="$n"
    break
  done
fi
if [ -z "$PC_IF" ]; then
  warn "no separate PC Ethernet interface found (eth1 / USB adapter missing)"
fi

echo "campus_if=$CAMPUS_IF pc_if=$PC_IF wlan_if=$WLAN_IF"

say "Stopping services"
systemctl stop campuspi 2>/dev/null || true
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
pkill -f '[p]ython3 /opt/campuspi/main.py' 2>/dev/null || true
sleep 1

say "Program files"
ls -l /opt/campuspi 2>/dev/null || true
if [ -d /opt/campuspi ]; then
  python3 -m py_compile /opt/campuspi/*.py 2>&1 || warn "py_compile failed"
  rm -rf /opt/campuspi/__pycache__
fi

say "Addresses"
if [ -n "$PC_IF" ]; then
  ip link set "$PC_IF" up 2>/dev/null || true
  ip addr flush dev "$PC_IF" 2>/dev/null || true
  ip addr add 192.168.9.1/24 dev "$PC_IF" 2>/dev/null || true
  echo "PC interface $PC_IF -> 192.168.9.1/24"
fi
if [ -n "$WLAN_IF" ]; then
  rfkill unblock wifi 2>/dev/null || true
  rfkill unblock all 2>/dev/null || true
  ip link set "$WLAN_IF" up 2>/dev/null || true
  ip addr flush dev "$WLAN_IF" 2>/dev/null || true
  ip addr add 192.168.10.1/24 dev "$WLAN_IF" 2>/dev/null || true
  echo "Wi-Fi interface $WLAN_IF -> 192.168.10.1/24"
fi

say "dnsmasq config"
if [ -n "$PC_IF" ] || [ -n "$WLAN_IF" ]; then
  backup "$DNSMASQ_CONF"
  {
    echo "no-resolv"
    echo "no-hosts"
    echo "log-dhcp"
    echo "server=223.5.5.5"
    echo "server=119.29.29.29"
    [ -n "$CAMPUS_IF" ] && echo "except-interface=$CAMPUS_IF"
    echo "address=/www.msftconnecttest.com/192.168.9.1"
    echo "address=/connectivitycheck.gstatic.com/192.168.9.1"
    if [ -n "$PC_IF" ]; then
      echo "interface=$PC_IF"
      echo "dhcp-range=interface:$PC_IF,192.168.9.100,192.168.9.150,255.255.255.0,2m"
      echo "dhcp-option=interface:$PC_IF,6,223.5.5.5,119.29.29.29"
    fi
    if [ -n "$WLAN_IF" ]; then
      echo "interface=$WLAN_IF"
      echo "dhcp-range=interface:$WLAN_IF,192.168.10.100,192.168.10.150,255.255.255.0,12h"
      echo "dhcp-option=interface:$WLAN_IF,3,192.168.10.1"
      echo "dhcp-option=interface:$WLAN_IF,6,192.168.10.1"
    fi
  } > "$DNSMASQ_CONF"
  cat "$DNSMASQ_CONF"
  dnsmasq --test 2>&1 || warn "dnsmasq --test failed"
else
  warn "no interfaces; dnsmasq config left unchanged"
fi

say "hostapd config"
PASSFILE="$BOOT_DIR/campuspi-hotspot-password.txt"
if [ -n "$WLAN_IF" ]; then
  backup "$HOSTAPD_CONF"
  PASS="$HOTSPOT_PASS"
  if [ ${#PASS} -lt 8 ] || [ ${#PASS} -gt 63 ]; then
    OLD_PASS="$(sed -n 's/^wpa_passphrase=//p' "$HOSTAPD_CONF.bak.$TS" 2>/dev/null | head -n 1)"
    if [ ${#OLD_PASS} -ge 8 ] && [ ${#OLD_PASS} -le 63 ]; then
      PASS="$OLD_PASS"
      echo "using existing hostapd password"
    else
      PASS="CampusPi-$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8)"
      echo "generated a temporary hotspot password"
    fi
  fi
  cat > "$HOSTAPD_CONF" <<EOF
interface=$WLAN_IF
driver=nl80211
ssid=$SSID
hw_mode=g
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
max_num_sta=4
auth_algs=1
wpa=2
wpa_passphrase=$PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
country_code=CN
EOF
  sed 's/^wpa_passphrase=.*/wpa_passphrase=***/' "$HOSTAPD_CONF"
  printf 'SSID: %s\nPassword: %s\nDelete this file after use.\n' "$SSID" "$PASS" > "$PASSFILE" 2>/dev/null || true
  chmod 600 "$PASSFILE" 2>/dev/null || true
  echo "hotspot password written to $PASSFILE"
else
  warn "no Wi-Fi interface; hostapd config left unchanged"
fi

if [ ! -f /etc/default/hostapd ]; then
  printf 'DAEMON_CONF="%s"\n' "$HOSTAPD_CONF" > /etc/default/hostapd
else
  backup /etc/default/hostapd
  if grep -q '^DAEMON_CONF=' /etc/default/hostapd; then
    sed -i 's|^DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
  else
    printf 'DAEMON_CONF="%s"\n' "$HOSTAPD_CONF" >> /etc/default/hostapd
  fi
fi

say "Starting dnsmasq"
systemctl unmask dnsmasq 2>/dev/null || true
systemctl enable dnsmasq 2>/dev/null || true
systemctl restart dnsmasq 2>&1 || true
sleep 1
systemctl is-active dnsmasq || true

say "Starting hostapd"
if [ -n "$WLAN_IF" ]; then
  systemctl unmask hostapd 2>/dev/null || true
  systemctl enable hostapd 2>/dev/null || true
  systemctl restart hostapd 2>&1 || true
  sleep 2
  systemctl is-active hostapd || true
  if ! systemctl is-active --quiet hostapd; then
    warn "hostapd is not active; last log:"
    journalctl -u hostapd -n 40 --no-pager 2>/dev/null || true
    if command -v hostapd >/dev/null 2>&1; then
      timeout 5 hostapd -dd "$HOSTAPD_CONF" 2>&1 | tail -n 80 || true
    fi
  fi
fi

say "Forwarding / NAT"
sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true
mkdir -p /etc/sysctl.d
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-campuspi.conf
iptables -P FORWARD ACCEPT 2>/dev/null || true
if [ -n "$CAMPUS_IF" ] && ip link show "$CAMPUS_IF" >/dev/null 2>&1; then
  iptables -t nat -C POSTROUTING -o "$CAMPUS_IF" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -o "$CAMPUS_IF" -j MASQUERADE 2>/dev/null || true
fi
if [ -n "$CAMPUS_IF" ]; then
  for a in "$PC_IF" "$WLAN_IF"; do
    [ -n "$a" ] || continue
    iptables -C FORWARD -i "$a" -o "$CAMPUS_IF" -j ACCEPT 2>/dev/null || \
      iptables -A FORWARD -i "$a" -o "$CAMPUS_IF" -j ACCEPT 2>/dev/null || true
    iptables -C FORWARD -i "$CAMPUS_IF" -o "$a" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
      iptables -A FORWARD -i "$CAMPUS_IF" -o "$a" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  done
fi
if [ -n "$PC_IF" ] && [ -n "$WLAN_IF" ]; then
  iptables -C FORWARD -i "$PC_IF" -o "$WLAN_IF" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "$PC_IF" -o "$WLAN_IF" -j ACCEPT 2>/dev/null || true
  iptables -C FORWARD -i "$WLAN_IF" -o "$PC_IF" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "$WLAN_IF" -o "$PC_IF" -j ACCEPT 2>/dev/null || true
fi

say "Starting campuspi"
systemctl enable campuspi 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl restart campuspi 2>&1 || true
sleep 3
systemctl is-active campuspi || true
if ! systemctl is-active --quiet campuspi; then
  warn "campuspi did not stay active; foreground output:"
  timeout 10 python3 /opt/campuspi/main.py 2>&1 | tail -n 120 || true
  systemctl restart campuspi 2>&1 || true
  sleep 2
fi

say "SSH"
systemctl enable ssh 2>/dev/null || true
systemctl start ssh 2>/dev/null || true
systemctl is-active ssh || true

say "Verification"
ip -br addr 2>/dev/null || true
ip route 2>/dev/null || true
systemctl is-active campuspi dnsmasq hostapd ssh 2>/dev/null || true
ss -lunp 2>/dev/null | grep -E ':(53|67|68)([[:space:]]|$)' || true
ss -ltnp 2>/dev/null | grep ':8080' || true
echo "--- dnsmasq ---"
cat "$DNSMASQ_CONF" 2>/dev/null || true
echo "--- hostapd (password redacted) ---"
sed 's/^wpa_passphrase=.*/wpa_passphrase=***/' "$HOSTAPD_CONF" 2>/dev/null || true
echo "--- campuspi log ---"
tail -n 100 /var/log/campuspi.log 2>/dev/null || true
echo "--- campuspi journal ---"
journalctl -u campuspi -n 100 --no-pager 2>/dev/null || true
echo "--- iw ---"
iw dev 2>/dev/null || true

say "Summary"
if [ -n "$PC_IF" ] && ip -4 addr show "$PC_IF" 2>/dev/null | grep -q '192\.168\.9\.1'; then
  echo "PC Ethernet ($PC_IF) is 192.168.9.1/24."
  echo "Connect the PC to the USB Ethernet adapter; it should get 192.168.9.100-150."
  echo "Web UI: http://192.168.9.1:8080"
else
  echo "PC Ethernet was not configured (no USB adapter found or cable is on eth0)."
fi
if [ -n "$WLAN_IF" ] && ip -4 addr show "$WLAN_IF" 2>/dev/null | grep -q '192\.168\.10\.1'; then
  echo "Hotspot ($WLAN_IF) is 192.168.10.1/24, SSID=$SSID."
  echo "Web UI via hotspot: http://192.168.10.1:8080"
fi
if [ -f "$PASSFILE" ]; then
  echo "Hotspot password file: $PASSFILE (delete it after use)."
fi
echo "Log: $LOG"
echo "If services are active but clients still fail, reboot once: sudo reboot"
