# Raspberry Pi HQ Camera (IMX477) on the ROCK 5T

Goal: run the two genlocked Pi HQ cameras on the **Radxa ROCK 5T** (RK3588).
Three deliverables, none of which exist today: a **kernel driver**, a
**device-tree overlay**, and an **ISP tuning (IQ) file**. The XVS genlock
validated on the Orin (see `RPI_HQ_CAMERA.md`) is sensor-side and ports
straight into the new driver.

## Board recon (2026-09-03, live from the device)

| Item | Found |
|---|---|
| Board | Radxa ROCK 5T (`/proc/device-tree/model`) — NOT the 5B+ the flex adapter targeted; verify camera connectors before ordering cables |
| OS / kernel | Debian 12, vendor kernel `6.1.84-8-rk2410` |
| Kernel headers | installed (`linux-headers-6.1.84-8-rk2410`) → out-of-tree module builds work on-device |
| Build tools | gcc, make, git all present |
| ISP stack | `camera-engine-rkaiq 6.8.0-rk3588`, matches **rkisp v30** → that's the IQ JSON schema to target |
| IQ files | `/etc/iqfiles/` has imx219 (RPi cam v2), ov5647 (RPi cam v1), imx415 4K, others — **no imx477** |
| Camera drivers | built into the kernel (`=y`, not modules): imx219, imx415, imx464, imx214, **imx577** |
| IMX477 driver | none, anywhere |
| Overlays | `/boot/dtbo/` all generic/disabled; radxa-overlays source has camera overlays only for CM3-series boards — none for 5B/5T |
| Overlay management | `rsetup` (Radxa's tool) + `/boot/dtbo/` |
| `/dev/video0` | `stream_hdmirx` (the 5T's HDMI input — not a camera) |

## Why this is tractable

1. **`CONFIG_VIDEO_IMX577=y`** — Rockchip's vendor kernel already carries a
   driver for the IMX577, the IMX477's near-identical sibling (same 12.3MP
   Sony family). The IMX477 driver should be a light adaptation of that code
   (chip-ID, mode tables), not a from-scratch port. Rockchip sensor drivers
   also carry the RKMODULE ioctls the rkisp/rkaiq stack uses to find the
   right IQ file — mimic imx577/imx415, don't transplant the RPi driver.
2. **Radxa already tunes RPi cameras** (v1/v2 IQ files ship in the image), so
   RPi-camera-on-Rock is a trodden path — just not yet for the HQ camera.
3. **RPi publishes the IMX477's lab calibration** (`imx477.json` in the
   libcamera/raspberrypi repos): black level, noise-vs-gain model, AWB CT
   curve, CCMs at ~8 color temperatures, gamma. The IQ file is a translation
   into the rkisp v30 JSON, not a re-measurement.

## Bring-up log (2026-09-04): software stack VALIDATED on hardware, awaiting cameras

First install on the actual ROCK 5T (kernel 6.1.84-8-rk2410) — everything
that can be proven without cameras is proven:

- **Driver compiles clean** first try against the installed headers (only the
  cosmetic Debian gcc point-release warning); `imx477.ko` vermagic matches the
  running kernel; `trigger_mode` + `dpc_enable` params present.
- **Overlay compiles** (only cosmetic graph_child_address warnings) to a 10.5KB
  dtbo. Activated via `u-boot-update` (this image retired uEnv.txt; overlays =
  every non-`.disabled` *.dtbo in /boot/dtbo/, baked into extlinux.conf's
  `fdtoverlays` line).
- **After reboot:** overlay applied — both chains live (`rkcif-mipi-lvds2` +
  `rkcif-mipi-lvds4`) and **both** ISP mainpaths registered (`rkisp0-vir0`
  video22-28, `rkisp1-vir1` video31-37). Driver auto-loaded via modalias,
  bound BOTH nodes (`imx477 3-001a`, `imx477 4-001a`), fell back gracefully on
  the absent reset/pwdn GPIOs + regulators (Pi HQ self-powers — by design),
  reached the chip-ID read and reported `Unexpected sensor id(0000), ret(-5)`
  on both buses — the correct "no sensor connected" signal.

**Install steps that worked** (from ~/orin-recorder/rock5t-camera):
```
cd driver && make && sudo cp imx477.ko /lib/modules/$(uname -r)/kernel/drivers/media/i2c/ && sudo depmod -a
sudo cp iqfiles/imx477_RPI-HQ_default.json /etc/iqfiles/
cd overlay && H=/usr/src/linux-headers-$(uname -r); cpp -nostdinc -I "$H/include" -undef -x assembler-with-cpp rock-5t-dual-rpi-hq-imx477.dts | dtc -I dts -O dtb -@ -o rock-5t-dual-rpi-hq-imx477.dtbo
sudo cp rock-5t-dual-rpi-hq-imx477.dtbo /boot/dtbo/ && sudo u-boot-update && sudo reboot
```

**At cable time**, expected success: `imx477 N-001a: ... sensor id 0x0477`,
probe succeeds, `i2cdetect -y 3` / `-y 4` show `UU` at 0x1a, video pipeline
completes. Then: `v4l2-ctl` raw smoke test -> rkaiq/ISP path (uses the IQ
file) -> port sync_test.sh for genlock. If probe still reads 0000 WITH a
camera attached, suspect cable/connector seating first, then the RPi-HQ R8
power-down issue (see rpi-hq-camera-orin memory / RidgeRun).

## Status (2026-09-03): all three pieces DRAFTED, awaiting hardware

- **Driver**: `rock5t-camera/driver/imx477.c` + Makefile + NOTES.md — Rockchip
  imx577 body + RPi imx477 sensor facts + XVS genlock via DT `trigger-mode`.
  NOT yet compiled — first action next time the Rock is on: `make` against the
  installed headers (expected 1-line fixups listed in NOTES.md).
- **Overlay**: `rock5t-camera/overlay/rock-5t-dual-rpi-hq-imx477.dts` — both
  cameras, genlock roles baked in (CAM0 source, CAM1 sink). Chains verified
  TWICE: schematic sheet 18 + Radxa's own upstream rock-5t camera overlays
  (which the installed radxa-overlays 0.2.27 predates — `apt upgrade` gets
  Radxa's stock ones too). Build/install: overlay/README.md.
- **IQ file**: `rock5t-camera/iqfiles/imx477_RPI-HQ_default.json` — skeleton
  switched to Radxa's shipping **imx577** IQ (sibling sensor; BLC cross-
  validates RPi's to the LSB). RPi lab data transplanted: AWB gains, 14 CCMs,
  gamma. LSC neutral (per-lens, later). Regenerate via gen_imx477_iq.py.
- **Bring-up order** (Rock on, cameras cabled): compile driver -> install
  .ko + .dtbo + IQ json -> reboot -> i2cdetect 0x1a on buses 3 & 4 ->
  v4l2 raw smoke test -> rkaiq/ISP path -> port sync_test.sh for genlock proof.

## Work plan

### 1. Kernel driver (out-of-tree module `imx477.ko`)
- Base: Rockchip's `imx577.c` from the vendor kernel source
  (github.com/radxa/kernel, the 6.1 rkr branch matching `6.1.84-8-rk2410`).
- Adapt: chip ID (0x0477), mode tables (full 4056x3040, 2x2 binned 2028x1520,
  1080p), link freq for the connector's lane count.
- Bake in the XVS genlock from the Orin work: registers 0x3F0B/0x3041/0x3040/
  0x4B81, source=1/1/1/1 sink=1/0/0/0, applied at stream-on in standby.
  Role selection via a DT property (`trigger-mode = "source"|"sink"`) per
  camera node. NEVER both source (bus contention on the shared XVS wire).
- Build on the 5T against the installed headers; load via
  `/etc/modules-load.d/`.

### 2. Device-tree overlay
- No 5T camera overlay exists to copy — author from `rk3588-rock-5t.dts`
  (vendor kernel source) to find each CSI connector's i2c bus, dphy/csi2
  host, clock and power rails.
- Model on the CM3 rpi-camera overlays + the 5B radxa-camera-4k pattern:
  sensor node (i2c addr 0x1a, 24MHz xclk) -> csi2_dphy -> mipi2_csi2 ->
  rkcif -> rkisp virtual nodes.
- OPEN QUESTION: 5T connector pinout/lane count vs the Pi HQ's 2-lane, 15-pin
  FPC — determines the adapter cable AND whether the rock-5-pcb flex design
  (drawn for the 5B+ 31-pin CAM0) needs rework for the 5T.

### 3. IQ / tuning file (`imx477_RPI-HQ_default.json`)
- Skeleton: `/etc/iqfiles/imx415_RADXA-CAMERA-4K_DEFAULT.json` (4K sensor,
  same rkisp v30 schema).
- Transplant from RPi's `imx477.json`: black level, noise model, AWB
  calibration, CCMs, gamma.
- Daylight-first simplification: the rig records outdoors (sun / partly
  cloudy, ~5500-6800K). One daylight CCM + fixed daylight AWB gains covers
  the whole operating range; skip the tungsten-to-shade table initially.
- Lens shading: ships neutral at first; calibrate later from a flat-field
  shot with the actual lenses (matters at the stitch seam).
- Verification without a color chart: shoot the same scene with the Orin
  pipeline (same sensors+lenses, trusted tuning) and fit the residual
  correction — the same method that produced `grade.sh`'s matrix.

### 4. Cross-checks once cables exist
- `i2cdetect` for 0x1a on the connector's bus, then driver probe.
- Streaming smoke test: `v4l2-ctl` raw frames, then rkaiq path.
- Genlock: port `recorder/sync_test.sh` (timestamp drift measurement) — the
  method is platform-neutral.

## Access
- SSH from the Mac: `ssh rock` (radxa@192.168.86.136, key auth works).
