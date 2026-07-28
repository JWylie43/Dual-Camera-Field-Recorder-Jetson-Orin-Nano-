#!/usr/bin/env bash
#
# orin-automount.sh - mount the field SSD at /mnt/usb when it's plugged in.
#
# Triggered by udev (99-orin-automount.rules -> orin-automount.service) whenever
# the SSD with the matching UUID is plugged into the Orin. It ONLY mounts the
# drive - it does NOT copy anything. Run the transfer yourself when you're ready:
#
#     sudo /usr/local/bin/offload.sh        (or field-offload/offload.sh)
#
# Matched by filesystem UUID = unique to THIS drive (the label "Extreme SSD" is
# the SanDisk factory default, shared by every Extreme). Find it with `lsblk -f`.
#
set -uo pipefail

# ---- config -------------------------------------------------------------
UUID="5E64-018F"            # this SSD's exFAT UUID (lsblk -f)
MNT="/mnt/usb"              # mountpoint (same one the manual workflow uses)
LOG="/var/log/orin-automount.log"
# -------------------------------------------------------------------------

exec >>"$LOG" 2>&1
echo "==== $(date '+%F %T')  SSD plugged in (uuid=$UUID) ===="

DEV="/dev/disk/by-uuid/${UUID}"

# The by-uuid symlink can lag the plug event by a moment - wait briefly.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -e "$DEV" ] && break
    sleep 0.5
done
if [ ! -e "$DEV" ]; then
    echo "no device with UUID '${UUID}' appeared; nothing to do."
    exit 0
fi

mkdir -p "$MNT"
if mountpoint -q "$MNT"; then
    echo "already mounted at $MNT."
    exit 0
fi

# kernel exfat driver first, FUSE helper as fallback (older L4T)
mount "$DEV" "$MNT" 2>/dev/null \
    || mount -t exfat "$DEV" "$MNT" 2>/dev/null \
    || mount.exfat-fuse "$DEV" "$MNT"

if mountpoint -q "$MNT"; then
    echo "mounted at $MNT - ready. Run 'sudo offload.sh' to copy."
else
    echo "ERROR: could not mount $DEV at $MNT (is exfat-fuse/exfatprogs installed?)."
    exit 1
fi
