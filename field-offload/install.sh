#!/usr/bin/env bash
#
# install.sh - install the auto-offload-on-plug service on the Orin.
# Run on the Orin:  sudo ./field-offload/install.sh
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./field-offload/install.sh" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing offload script -> /usr/local/bin/orin-offload.sh"
install -m 0755 "$HERE/orin-offload.sh" /usr/local/bin/orin-offload.sh

echo "==> Installing systemd service -> /etc/systemd/system/orin-offload.service"
install -m 0644 "$HERE/orin-offload.service" /etc/systemd/system/orin-offload.service
systemctl daemon-reload

echo "==> Installing udev rule -> /etc/udev/rules.d/99-orin-offload.rules"
install -m 0644 "$HERE/99-orin-offload.rules" /etc/udev/rules.d/99-orin-offload.rules
udevadm control --reload

echo
echo "Matched by this drive's filesystem UUID (5E64-018F) - unique to it."
echo "Confirm it still matches:  lsblk -f   (the UUID column for your SSD)."
echo "If you ever reformat/replace the SSD, update the UUID in BOTH"
echo "orin-offload.sh and 99-orin-offload.rules, then re-run this installer."
echo
echo "Then just plug the SSD in. Watch it:  tail -f /var/log/orin-offload.log"
