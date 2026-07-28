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
echo "Done. Make sure your SSD's exFAT label matches the one in the rule/script"
echo "(default: ORINDUMP). Check with:  lsblk -f"
echo "Set it (drive unmounted) with:    sudo exfatlabel /dev/sda1 ORINDUMP"
echo
echo "Then just plug the SSD in. Watch it:  tail -f /var/log/orin-offload.log"
