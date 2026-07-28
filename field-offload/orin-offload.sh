#!/usr/bin/env bash
#
# orin-offload.sh - copy everything in /mnt/video onto a USB SSD, automatically.
#
# Triggered by udev (99-orin-offload.rules -> orin-offload.service) whenever the
# SSD with the matching filesystem LABEL is plugged into the Orin. It mounts the
# SSD, rsyncs the whole recordings folder onto it (incrementally - re-plugging
# only copies new files), then unmounts so it's safe to yank.
#
# SAFETY: this only ever COPIES. It never deletes from the Orin - that stays a
# manual step after you've verified the copy (the project's golden rule). Only
# the drive whose label matches LABEL below can trigger it, so a random USB stick
# does nothing.
#
# Watch it work:   tail -f /var/log/orin-offload.log
#
set -uo pipefail

# ---- config -------------------------------------------------------------
LABEL="ORINDUMP"            # your SSD's exFAT label (see field-offload/README)
SRC="/mnt/video"            # where recordings live
MNT="/mnt/usb"              # mountpoint (same one the manual workflow uses)
DEST_SUBDIR="orin-video"    # folder created on the SSD
LOG="/var/log/orin-offload.log"
# -------------------------------------------------------------------------

exec >>"$LOG" 2>&1
echo "==== $(date '+%F %T')  offload triggered (label=$LABEL) ===="

DEV="/dev/disk/by-label/${LABEL}"

# The by-label symlink can lag the plug event by a moment - wait briefly.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -e "$DEV" ] && break
    sleep 0.5
done
if [ ! -e "$DEV" ]; then
    echo "no device with label '${LABEL}' appeared; nothing to do."
    exit 0
fi

# Always leave the drive unmounted on the way out, however we exit.
cleanup() {
    sync
    if mountpoint -q "$MNT"; then
        umount "$MNT" && echo "unmounted $MNT - safe to unplug."
    fi
}
trap cleanup EXIT

mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
    # kernel exfat driver first, FUSE helper as fallback (older L4T)
    mount "$DEV" "$MNT" 2>/dev/null \
        || mount -t exfat "$DEV" "$MNT" 2>/dev/null \
        || mount.exfat-fuse "$DEV" "$MNT"
fi
if ! mountpoint -q "$MNT"; then
    echo "ERROR: could not mount $DEV at $MNT (is exfat-fuse/exfatprogs installed?)."
    exit 1
fi

DEST="${MNT}/${DEST_SUBDIR}"
mkdir -p "$DEST"
echo "copying ${SRC}/ -> ${DEST}/  (incremental)"
rsync -ah --info=progress2 "${SRC}/" "${DEST}/"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "WARNING: rsync exited $rc - some files may not have copied. NOT deleting anything."
else
    echo "copy complete."
fi
echo "==== $(date '+%F %T')  offload done ===="
# cleanup() runs on EXIT and unmounts.
