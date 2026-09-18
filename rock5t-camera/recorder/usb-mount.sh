#!/usr/bin/env bash
# rock-usb-mount - mount/unmount a removable partition at /mnt/usb.
#
# Called by rock_server.py through sudo, because the panel runs unprivileged.
# INSTALL AS ROOT-OWNED (a radxa-writable script behind NOPASSWD sudo would be
# a privilege-escalation hole):
#
#   sudo install -o root -g root -m 755 usb-mount.sh /usr/local/sbin/rock-usb-mount
#   echo 'radxa ALL=(root) NOPASSWD: /usr/local/sbin/rock-usb-mount' \
#     | sudo tee /etc/sudoers.d/rock-usb-mount
#   sudo chmod 440 /etc/sudoers.d/rock-usb-mount
#
# Usage: rock-usb-mount mount /dev/sdXN | rock-usb-mount umount
set -euo pipefail

MNT=/mnt/usb
OWNER=${SUDO_USER:-radxa}

case "${1:-}" in
  mount)
    dev="${2:-}"
    # only removable-disk partitions; never a system disk (nvme, mmcblk)
    [[ "$dev" =~ ^/dev/sd[a-z][0-9]+$ ]] || { echo "refusing: '$dev' is not a /dev/sdXN partition"; exit 1; }
    [[ -b "$dev" ]] || { echo "no such block device: $dev"; exit 1; }
    mkdir -p "$MNT"
    if mountpoint -q "$MNT"; then echo "already mounted at $MNT"; exit 0; fi
    fstype=$(lsblk -no FSTYPE "$dev" | head -1)
    # vfat/exfat/ntfs have no unix ownership: map the whole fs to the panel user
    case "$fstype" in
      exfat|vfat|ntfs|ntfs3)
        mount -o "uid=$(id -u "$OWNER"),gid=$(id -g "$OWNER"),noatime" "$dev" "$MNT" ;;
      *)
        mount -o noatime "$dev" "$MNT"
        chown "$OWNER" "$MNT" 2>/dev/null || true ;;
    esac
    echo "mounted $dev ($fstype) -> $MNT"
    ;;
  umount)
    sync
    umount "$MNT"
    echo "unmounted $MNT - safe to unplug"
    ;;
  *)
    echo "usage: $0 mount /dev/sdXN | $0 umount"
    exit 1
    ;;
esac
