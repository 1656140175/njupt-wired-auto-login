#!/bin/sh
# Stabilize the r8152 USB Ethernet adapter (eth1):
#   - disable EEE (Energy Efficient Ethernet), a known cause of
#     "NETDEV WATCHDOG: eth1 (r8152): transmit queue timed out"
#   - disable USB autosuspend for the adapter
#
# Idempotent and safe to run repeatedly.

set -u

IFACE="${1:-eth1}"

# Wait up to 30 seconds for the USB Ethernet interface to appear.
i=0
while [ ! -e "/sys/class/net/$IFACE" ] && [ "$i" -lt 15 ]; do
    sleep 2
    i=$((i + 1))
done
[ -e "/sys/class/net/$IFACE" ] || exit 0

ETHTOOL="$(command -v ethtool 2>/dev/null || true)"
[ -n "$ETHTOOL" ] || ETHTOOL=/usr/sbin/ethtool
"$ETHTOOL" --set-eee "$IFACE" eee off 2>/dev/null || true

# Disable USB autosuspend on the parent USB device of the network interface.
DEV="$(readlink -f "/sys/class/net/$IFACE/device" 2>/dev/null || true)"
if [ -n "$DEV" ]; then
    USB="$(dirname "$DEV")"
    if [ -w "$USB/power/control" ]; then
        echo on > "$USB/power/control" 2>/dev/null || true
    fi
fi

exit 0
