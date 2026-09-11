# Raspberry Pi HQ Camera (IMX477) on the ROCK 5T

> **Resuming this work (e.g. cables arrived)? Start with
> [`NEXT_STEPS.md`](NEXT_STEPS.md)** — a self-contained handoff: current state,
> how to operate the board, and the exact next-step sequence.


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

## Bring-up log (2026-09-11): cameras cabled — probe PASSES, async-bind race found + fixed

Both Pi HQ cameras connected (CAM0 J5002, CAM1 J10) with the new 30-pin cables.

- **STEP 2 PASS:** `Detected Sony imx0477 sensor` on `3-001a` AND `4-001a`;
  `i2cdetect` shows `UU` at 0x1a on buses 3 and 4. Cables, wiring, overlay
  chains, driver probe: all good. No R8 power-down issue.
- **New blocker found: module load-order race.** The sensor entities never
  appeared in the CIF media graphs (`csi2-dphy0/4` sink pads unlinked), so
  rkaiq found no camera (and segfaulted — it crashes on an empty sensor list)
  and mainpath STREAMON returned EPERM (`check rkisp_mainpath link or isp
  input`). Root cause, from dmesg timeline + vendor `phy-rockchip-csi2-dphy.c`:
  the D-PHY registers its sensor async-notifier at probe (~11.8s), and at
  ~11.87s rkcif/rkisp run Rockchip's **"clear unready subdev"** — dropping the
  not-yet-arrived sensor and force-completing the notifiers. Out-of-tree
  `imx477.ko` loads via udev at ~14.5s: registers fine, matches nothing, ever.
  Radxa's own cameras never hit this because their sensor drivers are **built
  into the kernel** (`=y`) — only an out-of-tree sensor module can lose this
  race. (Phandle fixups, DT graph, CONFIG_NO_GKI, driver binding: all verified
  fine along the way.)
- **Dead end, do not retry:** unbind/rebind of `csi2-dphy0` at runtime → kernel
  oops (vendor rkcif/rkisp keep stale refs into the D-PHY after notifier
  completion). Reboot required after any such attempt.
- **Load-order fixes that DON'T work (tried, keep for the record):** modprobe
  softdep (parsed but never honored) and /etc/modules-load.d early static load
  (userspace itself starts too late). Root reason found by timeline + vendor
  source: the whole pipeline (dphy/csi2/cif/isp) is **builtin** (the .ko-looking
  entries are in modules.builtin), it probes during kernel init, and the
  "clear unready subdev" pass is a **late_initcall** — it runs BEFORE
  `Run /init` (11.98s vs 11.99s on this image). The vendor clear
  (`v4l2_async_notifier_clr_unready_dev`, CONFIG_NO_GKI) permanently
  `list_del`s the pending sensor asd, so a later registration matches nothing.
  **No module load ordering can ever win this race. The design assumes sensor
  drivers are builtin (Radxa's all are `=y`).**
- **Fix that works — split enable (now the required install):** the boot
  overlay leaves the ten v4l2 pipeline nodes disabled (nothing camera-related
  exists for the boot-time clear to purge); after boot,
  /etc/modules-load.d loads `imx477` then `rk_cam_defer_enable.ko`
  (driver/), which applies `overlay/rock-5t-cam-runtime-enable.dts` (installed
  as /lib/firmware/rock5t-cam-enable.dtbo) via `of_overlay_fdt_apply()`
  (EXPORT_SYMBOL_GPL; CONFIG_OF_OVERLAY=y on this kernel, no OF_CONFIGFS).
  The builtin drivers then probe with the sensors already registered.

  ```
  cd driver && make && sudo make install       # builds+installs both .ko
  cd ../overlay && dtc -I dts -O dtb -o rock5t-cam-enable.dtbo rock-5t-cam-runtime-enable.dts
  sudo cp rock5t-cam-enable.dtbo /lib/firmware/
  # rebuild + reinstall the boot dtbo (same cpp|dtc command as above)
  printf "imx477\nrk_cam_defer_enable\n" | sudo tee /etc/modules-load.d/imx477.conf
  sudo reboot
  ```

  Success signature after reboot: `rk_cam_defer_enable: applied ...`,
  `dphy0 matches m00_b_imx477 3-001a` (and dphy4/m01) in dmesg;
  `m0x_b_imx477` entities present in media graphs.
  **DKMS packaging must ship both modules, the runtime dtbo, and the
  modules-load.d file.**
- **Also found:** `rkaiq_3A.service` is broken as shipped (oneshot wrapper
  backgrounds the server, systemd then runs ExecStop = `killall`, so it dies
  after ~16ms). Run `sudo rkaiq_3A_server` manually for now; fix the unit
  (RemainAfterExit=yes or Type=forking) before relying on it.
- **Topology note for STEP 3:** this stack runs CIF→ISP **online** — the rkcif
  video nodes are not for raw capture here; the smoke test goes through
  `rkisp_mainpath` (video22 = CAM0, video31 = CAM1, NV12) with rkaiq_3A_server
  running. The raw-bypass grab in NEXT_STEPS STEP 3 doesn't apply as written.

## Bring-up log (2026-09-11, later): PIPELINE COMPLETE on custom builtin-driver kernel

The stock-kernel workaround attempts (see the earlier 2026-09-11 entry) kept
hitting new vendor races, so Joe chose the kernel route:
`kernel-build/build-kernel.sh` clones radxa/kernel **linux-6.1-stan-rkr4.1**
(= 6.1.84, matching the shipped 6.1.84-8-rk2410; rkr1 is 6.1.43 — wrong),
injects the driver in-tree, sets CONFIG_VIDEO_IMX477=y on the stock config,
and builds debs. Built in ~6 min in an arm64 Debian docker container on the
Mac M5 Pro (vs 1-2h native on the Rock) — container flow: clone in container
FS (not a bind mount), copy in /boot/config-* from the Rock + imx477.c,
`make -j18 bindeb-pkg LOCALVERSION=""`, docker cp the debs out, scp to Rock,
dpkg -i + u-boot-update (stock kernel remains the boot-menu fallback).

**First boot of 6.1.84-8-rk2410-imx477: complete success.** Sensors detect at
11.83s (kernel init), `dphy0 matches m00_b_imx477` / `dphy4 matches
m01_b_imx477`, and ALL FOUR notifiers complete (rkcif-mipi-lvds2/4 AND
rkisp0-vir0 / rkisp1-vir1 — the ISPs never completed on any stock-kernel
attempt). Full-enable overlay, zero runtime workarounds.

## Bring-up log (2026-09-11, night): FIRST LIGHT — both cameras imaging through the full ISP path

- rkaiq_3A fixed as a boot service (systemd drop-in, see kernel-build/README).
- Driver rev-2 (`trigger_mode` runtime override) built + installed; with the
  XVS pads not yet wired, `echo 0 > /sys/module/imx477/parameters/trigger_mode`
  free-runs both cameras (DT roles stay source/sink for genlock day).
- Both cameras capture clean 1080p NV12 from the mainpaths (video22/video31),
  steady at 10 fps = the sensor's default full-res 4056x3040@10 mode; the ISP
  scales. **4K30 mode selection is still TODO** (bake into record_dual.sh).
- **First-light images: sharp, detailed, correct geometry (inverted — cameras
  physically upside down), same scene from offset positions = stereo pair
  working.** Quality issues match the IQ TRANSLATION_NOTES predictions
  exactly: strong blue cast (AWB regions are imx577-module values — the
  flagged first recalibration) and dark indoors (8ms sports shutter cap +
  evening room light + aperture). Tune AWB via gen_imx477_iq.py in daylight;
  don't hand-edit the json.
- Grab-a-frame recipe: v4l2-ctl 60 frames to /tmp/*.nv12 (last frames are
  AE-converged), then
  `ffmpeg -f rawvideo -pix_fmt nv12 -s 1920x1080 -i X.nv12 -update 1 X.png`.

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
