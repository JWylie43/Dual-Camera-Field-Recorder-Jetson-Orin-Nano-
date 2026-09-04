#!/usr/bin/env python3
"""ae_follower.py - single-brain auto exposure for the genlocked stereo pair.

cam0 (the XVS genlock source) is the ONLY camera running auto exposure; this
script mirrors whatever exposure/gain rkaiq applies to cam0 onto cam1, so the
pair is matched in brightness the same way the XVS wire matches them in time.

DRAFT - written before first hardware bring-up. The mirroring mechanism
(direct V4L2 subdev control writes) assumes cam1's own AE is not fighting the
writes. If cam1's rkaiq re-applies its own AE each frame, the fallback plan is
to give cam1 a manual-AE IQ file: set a different lens-name in the overlay for
cam1 (e.g. "follow") so rkaiq loads imx477_RPI-HQ_follow.json - a copy of the
main IQ with CommCtrl.AecOpType set to manual. See TRANSLATION_NOTES.md.

Gain cap: the rig is daylight-only; gain adds noise, not light. The IQ file
already pins cam0's AE gain at 1.0 (ceiling 2.0 at full shutter only); this
script additionally clamps what it writes to cam1. GAIN_MAX is in linear
gain units (1.0 = unity). The driver's V4L2 gain control is linear 1/16
units (16 = 1.0x) per the Rockchip contract.

Env:
  GAIN_MAX=1.0    clamp for cam1's mirrored analogue gain (linear units)
  HZ=10           mirror rate (writes/second)

Usage: sudo ./ae_follower.py     (needs the two imx477 subdevs present)
"""
import glob
import os
import subprocess
import sys
import time

GAIN_MAX = float(os.environ.get("GAIN_MAX", "1.0"))
HZ = float(os.environ.get("HZ", "10"))
GAIN_CTRL_UNITY = 16                     # linear 1/16 units: 16 == 1.0x
GAIN_CTRL_MAX = int(GAIN_MAX * GAIN_CTRL_UNITY)


def find_imx477_subdevs():
    """Return (master, follower) subdev paths, ordered by rkaiq module index
    (m00 = cam0/i2c3 = genlock source = AE master)."""
    found = []
    for p in glob.glob("/sys/class/video4linux/v4l-subdev*"):
        try:
            name = open(os.path.join(p, "name")).read().strip()
        except OSError:
            continue
        if "imx477" in name:
            found.append((name, "/dev/" + os.path.basename(p)))
    found.sort()                          # "m00_b_imx477 3-001a" < "m01_..."
    if len(found) != 2:
        sys.exit(f"!! expected 2 imx477 subdevs, found {found} "
                 "(driver loaded? overlay active?)")
    print(f">> AE master:   {found[0][1]} ({found[0][0]})")
    print(f">> AE follower: {found[1][1]} ({found[1][0]})")
    return found[0][1], found[1][1]


def get_ctrl(dev, name):
    out = subprocess.run(["v4l2-ctl", "-d", dev, "--get-ctrl", name],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split(":")[1].strip())


def set_ctrl(dev, name, value):
    subprocess.run(["v4l2-ctl", "-d", dev, "--set-ctrl", f"{name}={value}"],
                   capture_output=True, check=True)


def main():
    master, follower = find_imx477_subdevs()
    print(f">> mirroring exposure+gain at {HZ} Hz, "
          f"gain clamped to {GAIN_MAX}x ({GAIN_CTRL_MAX} ctrl units)")
    last = (None, None)
    while True:
        try:
            exp = get_ctrl(master, "exposure")
            gain = min(get_ctrl(master, "analogue_gain"), GAIN_CTRL_MAX)
            if (exp, gain) != last:
                set_ctrl(follower, "exposure", exp)
                set_ctrl(follower, "analogue_gain", gain)
                print(f"   exp={exp} gain={gain}", flush=True)
                last = (exp, gain)
        except subprocess.CalledProcessError as e:
            # transient (stream restart etc.) - keep trying
            print(f"   ctrl error ({e}); retrying", flush=True)
            last = (None, None)
        time.sleep(1.0 / HZ)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n>> stopped")
