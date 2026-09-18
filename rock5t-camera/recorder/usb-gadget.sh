#!/usr/bin/env bash
# usb-gadget.sh - present the ROCK 5T to a USB-C host (your Mac) as a NETWORK
# adapter, so one cable gives a private high-speed link for pulling recordings.
#
#   sudo ./usb-gadget.sh up     # create + bind the gadget (survives until 'down')
#   sudo ./usb-gadget.sh down   # tear it down, port returns to normal host use
#   sudo ./usb-gadget.sh status
#
# Why network and not mass storage: mass storage hands the Mac a raw block
# device, and a filesystem may only be mounted by one side at a time - you'd
# have to unmount on the Rock before every transfer and risk corruption if you
# forgot. As a network device the Rock keeps recording while you scp.
#
# Mac side (one-time): System Settings > Network > the new USB interface >
# Configure IPv4 "Manually", IP 10.55.0.2, mask 255.255.255.0, no router/DNS.
# Then:  scp radxa@10.55.0.1:'~/recordings/take_*' ~/Desktop/
#
# NOTE: the Type-C port is dual-role, so it is either the gadget link OR a host
# port - if your USB drive lives there, move it to a blue USB-A 3.0 port.
set -euo pipefail

G=/sys/kernel/config/usb_gadget/rock
FUNC=${FUNC:-ncm}            # ncm = fast, macOS 12+; set FUNC=ecm if the Mac
                             # never shows an interface (older/pickier hosts)
IP=${IP:-10.55.0.1}
MASK=${MASK:-24}
IFACE=usb0

case "${1:-up}" in
up)
  modprobe libcomposite 2>/dev/null || true
  UDC=$(ls /sys/class/udc 2>/dev/null | head -1)
  [ -n "$UDC" ] || { echo "no USB device controller - gadget mode unavailable"; exit 1; }
  if [ -d "$G" ]; then echo "gadget already up"; exit 0; fi

  mkdir -p "$G"
  echo 0x1d6b > "$G/idVendor"          # Linux Foundation
  echo 0x0104 > "$G/idProduct"         # Multifunction Composite Gadget
  echo 0x0100 > "$G/bcdDevice"
  echo 0x0200 > "$G/bcdUSB"
  mkdir -p "$G/strings/0x409"
  echo "Radxa"                     > "$G/strings/0x409/manufacturer"
  echo "ROCK 5T stereo recorder"   > "$G/strings/0x409/product"
  echo "rock5t-0001"               > "$G/strings/0x409/serialnumber"

  mkdir -p "$G/configs/c.1/strings/0x409"
  echo "$FUNC network" > "$G/configs/c.1/strings/0x409/configuration"
  echo 250 > "$G/configs/c.1/MaxPower"

  mkdir -p "$G/functions/$FUNC.usb0"
  # fixed locally-administered MACs, so the Mac keeps the same network profile
  # (and its manual IP) across replugs instead of inventing a new interface
  echo "02:1a:11:00:00:01" > "$G/functions/$FUNC.usb0/dev_addr"
  echo "02:1a:11:00:00:02" > "$G/functions/$FUNC.usb0/host_addr"
  ln -s "$G/functions/$FUNC.usb0" "$G/configs/c.1/"

  echo "$UDC" > "$G/UDC"
  sleep 1
  ip addr add "$IP/$MASK" dev "$IFACE" 2>/dev/null || true
  ip link set "$IFACE" up
  echo "gadget up on $UDC ($FUNC), $IFACE = $IP/$MASK"
  echo "plug USB-C into the Mac, set its new interface to 10.55.0.2/255.255.255.0"
  ;;

down)
  if [ -d "$G" ]; then
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G/configs/c.1/$FUNC.usb0"
    rmdir "$G/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$G/configs/c.1" 2>/dev/null || true
    rmdir "$G/functions/$FUNC.usb0" 2>/dev/null || true
    rmdir "$G/strings/0x409" 2>/dev/null || true
    rmdir "$G" 2>/dev/null || true
    echo "gadget down"
  else
    echo "no gadget configured"
  fi
  ;;

status)
  echo -n "UDC:        "; ls /sys/class/udc 2>/dev/null || echo "(none)"
  echo -n "bound to:   "; cat "$G/UDC" 2>/dev/null || echo "(gadget not created)"
  echo -n "data_role:  "; cat /sys/class/typec/port0/data_role 2>/dev/null || echo "(n/a)"
  echo "iface:"; ip -br addr show "$IFACE" 2>/dev/null || echo "  ($IFACE not present)"
  ;;

*)
  echo "usage: $0 up | down | status"; exit 1 ;;
esac
