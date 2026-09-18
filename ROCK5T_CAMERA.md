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

## Bring-up log (2026-09-11, late): sensor modes proven; 4K30 has CRC noise, binned is clean

- Mode switching works: set the sensor subdev fmt, then **restart rkaiq_3A**
  before streaming (stale 3A state after a mode change = zero frames).
  `SENx=$(media-ctl -d /dev/mediaN -e "m0X_b_imx477 ...")` then
  `v4l2-ctl -d $SENx --set-subdev-fmt pad=0,width=W,height=H,code=C`
  (0x3012=SRGGB12 for full/binned, 0x300f=SRGGB10 for 4K30).
- **2028x1520@40 binned: flawless** (40.00fps, 25ms cadence, zero errors,
  900Mbps). This is the working recording mode today.
- **3840x2160@30: streams on BOTH cameras but with ~5k CSI CRC errors per
  30-frame burst (~7% of lines), near-identical counts on both cables →
  systematic, not one bad cable.** Candidate causes: cable/adapter SI margin
  at 2.1Gbps, or rkisp/dphy hs-settle timing for the 2096Mbps rate (software,
  affects both equally — check before buying cables). A still frame looks
  visually clean — CIF appears to absorb/drop damaged data; measure real
  delivered fps + motion artifacts before trusting 4K30.
- AWB: strong blue cast, as predicted (imx577 detection zones). Fix =
  daylight iteration session on gen_imx477_iq.py's AWB section (regenerate →
  /etc/iqfiles → restart rkaiq_3A → recapture; ~1min/iteration). Note: RPi's
  published AWB data cannot be converted to rkaiq's detection-zone format
  analytically — the zones live in rkaiq's own stats space (hence the
  imx577 placeholders).

## Bring-up log (2026-09-12): 4K30 CRC SOLVED — link re-clocked 2096 -> 1600Mbps/lane

Driver 4K30 mode re-clocked (kernel deb rev 3): IOP PLL 24/3*200 = 1600Mbps/lane
(800MHz link), hts 9024->11200 (per-line burst 1.44Gbps sustained, 11%
headroom), vts 3102->2500 (exactly 30.00 fps), MIPI global timing set to
sensor-auto (0x0808=0; the nv manual values were 2096-specific). First boot:
**data_rate 1600, 300/300 frames at 30.00 fps, ZERO CRC errors** (was
thousands at 2096 — cable SI margin, both cables identically). 4K30 is now
fully clean on the existing 30-pin FFC/adapters. Also validated this session:
Evbias -1.2 (accidental imx577 inheritance) -> 0; recording pipeline
(io-mode=dmabuf + queue) = timing-clean 4K30 H.265 recordings, all baked
into record_dual.sh.

## Bring-up log (2026-09-13): IQ texture round 1 VALIDATED; encoder bitrate root-caused

- Outdoor daylight eval (first fog was literally the window glass — shoot in
  open air). Baseline 1:1 crops: grass smeared to watercolor (spatial NR at
  full strength at base ISO) + sharpening halos (imx577 sharp_ratio=15).
- Round 1 (gen sec. 9): ynr/bayer2dnr low-ISO x0.5, cnr x0.6, sharp_ratio->6
  (ISO<=200 full, 400 tapered), dehaze+DRC disabled (scene-adaptive = pano
  seam poison). **A/B verdict: grass blades fully resolved, car edges crisp
  with zero halos. Round 1 stands.** Open round-2 candidate: shadows now run
  deep (honest tone without adaptive DRC) — judge on midday footage whether
  to add a small STATIC shadow lift via gamma.
- Encoder bitrate: mpph265enc silently ignores bps when caps carry no
  framerate (rkisp never advertises one) — 385-691Mbps files on detailed
  content; earlier in-spec files were just cheap dark scenes. Fix (verified
  27.4M vs 28M target): videorate ! framerate=N/1 before the encoder +
  rc-mode=cbr. In record_dual.sh.
- Tuning capture protocol: per scene, (1) pure video pipeline (true 30fps;
  a tee'd PNG branch starves the dmabuf pool and videorate then pads dups),
  (2) separate v4l2-ctl 90-frame burst -> last-frame PNG for pixel judgment.

## Bring-up log (2026-09-13, later): AE setpoint raised — CAPTURE-TIME CHECKLIST COMPLETE

Midday measurement: skeleton DySetpoint 16-20% (dropping when bright) left
sunny frames at mean 44/255 with only 0.25% clip = a wasted stop of headroom
(8-bit post-lifts cost quality). Raised to 26-30 (~+0.7EV): measured mean
44->62, crushed shadows 33%->11%, clip UNCHANGED 0.27%. Verified visually.

**Capture-time (unrecoverable) items now all handled:** highlight clipping
safe with margin; exposure target correct at capture; NR/sharpening mild
(post-safe direction) and validated; tone static (adaptive dehaze/DRC off —
shadow lift is now a Resolve slider, identical on both cams); WB locked
(uniform cast = one-slider post fix); bitrate honored; frame timing clean.
Shadow-lift question CLOSED by use-case: open field = only player shadows,
sunny or overcast — no deep-shade content; static tone + post grading covers
it. Field-side controls remaining: lens aperture + focus rings only.
Still open (not IQ): motion-blur/temporal-NR check on real moving subjects
(needs a motion video), LSC flat-field calibration, re-mount, XVS + sync.

## Bring-up log (2026-09-13, evening): lens HFOV MEASURED — 74° rig geometry validated

Wall measurement (Commonlands CIL391 3.25mm, 4K mode 3840 crop): W=83in at
D=29in -> **HFOV = 110.1° (+/-1.5°)**. With the 74° camera separation:
**~36° stitch overlap, ~184° total pano span** — the rig geometry closes
with margin. (Spec chain predicted 110-113°; old calibration jsons are the
1920x1200 Arducam rig — recalibrate intrinsics on these cameras before
stitching, with a strong-distortion model: lens is -16% TV barrel.)

Also found: **rkisp selfpath (video23) can hold a stale 1920x1080 crop
window instead of scaling** — preview showed a quarter of the scene 1:1
(and binned-mode recordings route via selfpath: set the crop selection
explicitly or verify FOV). Live preview workaround: stream MAINPATH
(video22) 4K->h264 10fps via udpsink to ffplay on the Mac. TODO: package
as preview.sh; add selfpath selection reset to record_dual.sh.

## Bring-up log (2026-09-18): intrinsics solved + FIRST DUAL 4K30 RECORDING

- **Intrinsics done** (cameras loose, focus locked): the CIL391 needs the
  FISHEYE model - the rational/pinhole fit leaves a uniform ~2.6px residual
  (model mismatch, FOV readout degenerates). cv2.fisheye: **RMS 0.233px
  (cam0) / 0.240px (cam1)**, f 2104/2108px (matched to 0.2%), FOV
  104.5x58.8 deg, 98% frame coverage, 46 views each. Per-unit decenter
  measured: cam0 cx 49px left (explains the asymmetric-baseboard look),
  cam1 cy 148px low - normal M12 tolerance, now calibrated rather than
  assumed. Saved: calibration/rock-rig/cam{0,1}_intrinsics.json.
  Board: charuco_board.png on a TV, 78mm squares (measured on glass).
  **Never touch the focus rings or reseat a lens** without recalibrating
  that camera.
- **calib_server.py / rock_server.py**: browser panels (8081 calibration,
  8080 recorder). Rules baked in: previews run CONTINUOUSLY on the selfpath
  and are never stopped/restarted (a camera switcher that restarted
  pipelines caused a lockup), snapshots/records come off the mainpath so
  both coexist, preview children run in their own process group and are
  killed on exit (orphans otherwise hold the nodes and silently black the
  next run), and startup refuses to run on top of leftovers.
- **FIRST SIMULTANEOUS DUAL 4K30 RECORDING (passed):** both cameras 238
  frames / 7.90s / **30.000 fps, zero gaps**, 27.3 + 27.2 Mbit vs the 28M
  target, clean decode. Two independent MKVs by design (fault isolation;
  the stitcher pairs full-res frames by timestamp).
- Browser note: Chrome can refuse LAN panels entirely
  (ERR_ADDRESS_UNREACHABLE) when a planted `LocalNetworkAccessAllowedForUrls`
  policy turns on Local Network Access enforcement. Safari is unaffected.

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
