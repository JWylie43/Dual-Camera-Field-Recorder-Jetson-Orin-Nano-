# NEXT STEPS — resume here when the camera cables arrive

**Read this first.** It is the single entry point for continuing the ROCK 5T
IMX477 bring-up in a fresh session, with no prior context assumed. Companion
docs: `ROCK5T_CAMERA.md` (plan + bring-up log), `SCHEMATIC_FACTS.md` (wiring),
`driver/NOTES.md`, `iqfiles/TRANSLATION_NOTES.md`, `overlay/README.md`.

## Where we are (2026-09-04)

Goal: two genlocked Raspberry Pi HQ cameras (Sony IMX477) on a **Radxa ROCK 5T**
(RK3588), one per CAM connector, for the stereo field recorder. The XVS hardware
genlock was already validated on the Orin (see the Orin side of this repo); this
branch ports the whole camera stack to the Rock.

**Status: the entire software stack is BUILT, INSTALLED, and VALIDATED on the
actual board — only the physical cameras are missing.** On the last boot the
driver loaded, bound both i2c buses, and probed to the chip-ID read, reporting
`Unexpected sensor id(0000)` on both — the correct "no camera connected" result.
Nothing else is blocking.

## Environment / how to operate (IMPORTANT)

- The Rock: **`ssh rock`** from Joe's Mac (user `radxa`, 192.168.86.136). Repo on
  the Rock: **`~/orin-recorder`**, branch `rock-5t-camera`. `git pull` there to
  get the latest.
- **The assistant's Bash tool CANNOT reach the Rock over the LAN** (sandbox
  network limit: gateway + internet work, LAN peer devices do not). So Rock
  commands are given to Joe, he runs them and pastes output back. This is the
  normal working mode for this task — don't burn time re-diagnosing it.
- `sudo` password is `radxa` (Joe plans to change it later).
- The assistant's Bash tool CAN reach GitHub — so `git` commits/pushes of source
  changes happen on the Mac normally; only on-Rock execution is relayed.

## Already installed on the Rock (from the last session)

- Driver: `imx477.ko` in `/lib/modules/6.1.84-8-rk2410/kernel/drivers/media/i2c/`
- IQ file: `/etc/iqfiles/imx477_RPI-HQ_default.json`
- Overlay: `/boot/dtbo/rock-5t-dual-rpi-hq-imx477.dtbo`, active via
  `u-boot-update` (baked into `/boot/extlinux/extlinux.conf` `fdtoverlays` line)

If the kernel was updated since, rebuild the driver (see `ROCK5T_CAMERA.md`
install steps) — a plain `.ko` breaks on kernel upgrades (this is why DKMS is on
the packaging TODO).

## STEP 1 — physical install

Power OFF the Rock. Connect one Pi HQ camera to **CAM0 (J5002)** and one to
**CAM1 (J10)**. Verify the adapter cable against `SCHEMATIC_FACTS.md` sheet-18
pinout (30-pin 0.5mm FPC, 2 lanes used: D0/D1 + CLK). Power on, `ssh rock`.

## STEP 2 — did the sensors probe?

```bash
sudo dmesg | grep -i imx477
sudo i2cdetect -y 3   # CAM0
sudo i2cdetect -y 4   # CAM1
```

- **SUCCESS:** dmesg shows `sensor id 0x0477` (not 0000) on 3-001a and 4-001a,
  probe succeeds; `i2cdetect` shows `UU` at `0x1a` on both buses.
- **FAIL (still id 0000 / no ACK):** the sensor isn't answering. Check, in order:
  (1) cable seating / orientation / connector latch; (2) power off/on (CSI is not
  hot-plug safe); (3) the genuine-RPi-HQ **R8 power-down resistor** issue — the
  Pi board can hold the sensor in power-down and the host doesn't release it (the
  Orin rig didn't need this mod, but flag it — see RidgeRun's IMX477-Jetson notes
  and the `rpi-hq-camera-orin` history).

## STEP 3 — raw capture smoke test (bypass the ISP first)

Confirm frames flow at the CSI/driver level before involving rkaiq. The rkcif/
rkisp video nodes from last boot: CAM0 = rkisp0-vir0 (video22-28),
CAM1 = rkisp1-vir1 (video31-37); re-check names with `v4l2-ctl --list-devices`.

```bash
v4l2-ctl -d /dev/video0 --list-formats-ext          # find the sensor's raw node/modes
# raw grab (pick the actual rkcif/sensor capture node):
v4l2-ctl -d <cif_node> --set-fmt-video=width=1920,height=1080 \
  --stream-mmap --stream-count=30 --stream-to=/tmp/cam0.raw
```

Modes the driver offers: 4056x3040@10, **3840x2160@30 (the recording mode)**,
2028x1520@40 binned, 1332x990@120. NOTE: 3840x2160@30 runs the MIPI link at
~2.1 Gbps/lane — if that mode shows corruption but binned modes are clean,
suspect **cable signal integrity**, not the driver (documented risk).

## STEP 4 — ISP path (first real image, exercises the IQ file)

Bring up the rkaiq pipeline (camera_engine_rkaiq 6.8.0 is installed). The IQ
file is matched by module name `RPI-HQ` (set in the overlay). Grab a JPEG
through the ISP and eyeball color/exposure. First-look checklist is in
`iqfiles/TRANSLATION_NOTES.md` — check in this order: black level (shadows not
milky/crushed), gray neutrality outdoors, then skin/grass/sky sanity. Expect
color to need a tweak; the generator `iqfiles/gen_imx477_iq.py` is the thing to
edit + rerun (don't hand-edit the json). Known first-tweak candidates:
AWB detection regions are imx577-module values (outdoor scenes ride the D50/D65
daylight fallback — set correctly, but first to recalibrate); LSC is neutral
(per-lens — calibrate from a flat-field shot).

## STEP 5 — genlock (the payoff)

Wire the two cameras' **XVS pads together + common ground** (1.8V, direct, no
level shift — same as the validated Orin rig). The overlay already assigns
roles: CAM0 = `trigger-mode = "source"` (master), CAM1 = `"sink"` (slave); the
driver applies the 4 registers (0x3F0B/0x3041/0x3040/0x4B81) at every stream
start in standby — so unlike the Orin, NO register pokes are needed, it comes up
genlocked. Prove it by porting `../recorder/sync_test.sh` (Orin) to the Rock's
v4l2 nodes: measure the cam0<->cam1 frame-timestamp offset; LOCKED = frozen
offset. (On the Orin the poke was needed because its driver never set the regs;
here the driver does it, so just start both streams and measure.)

## STEP 6 — recorder

Drafts exist, untested: `../recorder/record_dual.sh` (dual 4K30 H.265 via
mpph265enc, one MKV per camera, auto-discovers the mainpath nodes) and
`../recorder/ae_follower.py` (cam0 = the only AE, mirrors exposure/gain to cam1,
gain clamped). First runs will likely need element-name/device-node fixups
against what STEP 3-4 reveal. Design rationale (two files, single-brain AE,
8ms shutter cap for sports) is in those files' headers and the conversation.

## Open taste-calls (decide with real footage, not in the abstract)

- AE shutter cap is **8ms** (`gen_imx477_iq.py`, section 8) — revisit vs motion
  blur on a real game clip; 8 vs 12ms vs "prefer gain 2 sooner" is an eyeball call.
- Recording bitrate default 28 Mbit H.265/cam — confirm against footage.

## After it works: packaging for release

Plan discussed but not started (waits on a passing camera test): DKMS-package
the driver (survives kernel updates), install script, public README, sort the
IQ file license (or ship the generator instead of the baked json), then a repo
+ answer the Radxa forum thread; ideally upstream the driver+overlay to
`radxa-pkg/radxa-overlays` and the kernel. Details in the conversation.
