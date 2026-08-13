#!/usr/bin/env bash
#
# rig-launch.sh - launch the recorder app that matches the ACTIVE camera.
#
# camera-rig.service runs THIS instead of hardcoding one app, so switching
# cameras with `camswitch` (which reboots) brings up the right control panel on
# boot with no manual `systemctl` juggling:
#
#   imx477 overlay active + pi_app.py present -> pi_app.py  (Raspberry Pi HQ)
#   otherwise (arducam) + server.py present   -> server.py  (Arducam B0577)
#
# "Active camera" is read from the device-tree overlay camswitch selected, so it
# always agrees with the hardware that's actually enumerated.
#
# Install (branch-independent copy) + point the service at it - see comments at
# the bottom.

RECORDER_DIR="${RECORDER_DIR:-/home/joe/orin-recorder/recorder}"
CONF="${EXTLINUX_CONF:-/boot/extlinux/extlinux.conf}"

overlay="$(grep -E '^[[:space:]]*OVERLAYS' "$CONF" 2>/dev/null || true)"

if echo "$overlay" | grep -qi imx477 && [ -f "$RECORDER_DIR/pi_app.py" ]; then
    echo "rig-launch: imx477 active -> pi_app.py"
    exec python3 "$RECORDER_DIR/pi_app.py"
elif [ -f "$RECORDER_DIR/server.py" ]; then
    echo "rig-launch: arducam (or default) -> server.py"
    exec python3 "$RECORDER_DIR/server.py"
else
    echo "rig-launch: no recorder app found in $RECORDER_DIR (active overlay: $overlay)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# One-time setup on the Orin (branch-independent so it survives `git checkout`):
#
#   sudo cp ~/orin-recorder/recorder/rig-launch.sh /usr/local/bin/rig-launch
#   sudo chmod +x /usr/local/bin/rig-launch
#   sudo systemctl edit --full camera-rig.service     # set ExecStart to:
#       ExecStart=/usr/local/bin/rig-launch
#   sudo systemctl daemon-reload
#
# Leave the service ENABLED; it now self-selects the app per active camera.
# ---------------------------------------------------------------------------
