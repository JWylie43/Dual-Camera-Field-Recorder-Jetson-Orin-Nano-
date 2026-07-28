#!/usr/bin/env bash
#
# offload.sh - copy all recordings from /mnt/video onto the mounted SSD.
#
# You run this BY HAND when you want to transfer (the SSD auto-mounts on plug-in,
# but nothing copies until you run this). Copy-only: it never deletes from the
# Orin - verify, then delete originals yourself (the golden rule).
#
# Usage (on the Orin):   sudo ./field-offload/offload.sh
#
set -uo pipefail

SRC="/mnt/video"
MNT="/mnt/usb"
DEST="${MNT}/orin-video"

if ! mountpoint -q "$MNT"; then
    echo "SSD isn't mounted at $MNT."
    echo "Plug it in (it auto-mounts), or mount by hand:"
    echo "    sudo mount /dev/disk/by-uuid/5E64-018F $MNT"
    exit 1
fi

mkdir -p "$DEST"
echo "Copying $SRC/ -> $DEST/   (incremental, copy-only)"
rsync -ah --info=progress2 "$SRC/" "$DEST/"
rc=$?
echo
if [ "$rc" -eq 0 ]; then
    echo "Done. Verify the copy, then delete originals to free the NVMe:"
    echo "    sudo rm /mnt/video/game_YYYY-MM-DD_HH-MM-SS*"
else
    echo "rsync exited $rc - some files may not have copied. Nothing was deleted."
fi
exit "$rc"
