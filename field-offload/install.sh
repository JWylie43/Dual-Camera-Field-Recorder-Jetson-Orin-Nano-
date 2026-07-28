#!/usr/bin/env bash
#
# install.sh - install the auto-MOUNT-on-plug service + the manual offload script.
# Run on the Orin:  sudo ./field-offload/install.sh
#
# Result: plugging in the SSD auto-mounts it at /mnt/usb (no copy). You then run
# `sudo offload.sh` whenever you want to transfer.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./field-offload/install.sh" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Removing any older auto-COPY install (orin-offload.*), if present..."
# The first version auto-copied on plug-in; the current one only mounts. Clean it
# out so a reinstall migrates cleanly and nothing auto-transfers behind your back.
systemctl disable --now orin-offload.service >/dev/null 2>&1 || true
rm -f /usr/local/bin/orin-offload.sh \
      /etc/systemd/system/orin-offload.service \
      /etc/udev/rules.d/99-orin-offload.rules

echo "==> Installing mount script   -> /usr/local/bin/orin-automount.sh"
install -m 0755 "$HERE/orin-automount.sh" /usr/local/bin/orin-automount.sh

echo "==> Installing offload script -> /usr/local/bin/offload.sh"
install -m 0755 "$HERE/offload.sh" /usr/local/bin/offload.sh

echo "==> Installing systemd service -> /etc/systemd/system/orin-automount.service"
install -m 0644 "$HERE/orin-automount.service" /etc/systemd/system/orin-automount.service
systemctl daemon-reload

echo "==> Installing udev rule -> /etc/udev/rules.d/99-orin-automount.rules"
install -m 0644 "$HERE/99-orin-automount.rules" /etc/udev/rules.d/99-orin-automount.rules
udevadm control --reload

echo
echo "Done. Keyed to this drive's UUID (5E64-018F) - confirm with: lsblk -f"
echo "If you reformat/replace the SSD, update the UUID in orin-automount.sh AND"
echo "99-orin-automount.rules, then re-run this installer."
echo
echo "Plug the SSD in  -> it auto-mounts at /mnt/usb  (watch: tail -f /var/log/orin-automount.log)"
echo "Transfer when ready:  sudo offload.sh"
