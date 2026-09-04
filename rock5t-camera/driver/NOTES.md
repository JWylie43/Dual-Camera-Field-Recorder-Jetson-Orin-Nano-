# imx477 driver for Rockchip RK3588 (ROCK 5T) — merge notes

Target: Radxa ROCK 5T, vendor kernel 6.1.84-8-rk2410, built out-of-tree on
the board. `compatible = "sony,imx477"`.

## What came from where

**Rockchip imx577 vendor driver** (`reference/imx577_rockchip_driver.c`) —
the structural base:
- probe/remove flow, mutex/ctrl-handler/subdev/media-pad setup, runtime PM
- power on/off sequence skeleton (power/reset/pwdn GPIOs, regulators
  avdd/dovdd/dvdd, pinctrl `rockchip,camera_default`/`_sleep`)
- i2c read/write helpers and REG_NULL-terminated `struct regval` tables
- v4l2 subdev ops layout, `get_mbus_config`, `get_selection` (incl. the
  4056 -> 4048 CROP_BOUNDS alignment trick), enum_frame_interval with
  `reserved[0] = hdr_mode`
- the full RKMODULE ioctl block + compat_ioctl32: GET_MODULE_INFO,
  GET/SET_HDR_CFG, AWB_CFG, LSC_CFG, SET_QUICK_STREAM, GET_CHANNEL_INFO
- `rockchip,camera-module-*` DT property parsing and the
  `m%02d_%s_imx477 ...` subdev naming rkaiq expects

**Raspberry Pi imx477 driver** (`reference/imx477_rpi_driver.c`) — all
sensor facts:
- chip ID 0x0477 at reg 0x0016
- `mode_common_regs` (one-time init table) and the three mode tables
- exposure (0x0202, offset 22, min 4), analog gain register facts (0x0204,
  code 0..978, gain = 1024/(1024-code) — but see deviation 8: the V4L2
  control contract is the Rockchip linear one), digital gain (0x020e, 8.8
  fixed point)
- VTS via 0x0340 with the long-exposure shift register 0x3100
  (`long_exp_shift`, max 7)
- on-sensor DPC regs 0x0b05/0x0b06 (module param `dpc_enable`, default on)
- XVS trigger register set 0x3f0b / 0x3041 / 0x3040 / 0x4b81 and the
  stop-stream EXTOUT_EN=0 write
- XCLR power-on delay (8ms, datasheet T7)

**NVIDIA/RidgeRun nv_imx477 driver** (`imx477_mode_tbls.h`, fetched from
the L4T public sources mirror
`github.com/Consti10/Linux_for_Tegra .../kernel/nvidia/drivers/media/i2c/`,
RidgeRun copyright 2020 — the same nv_imx477 already validated on the
Jetson Orin) — source for the 3840x2160@30 mode:
- its `imx477_mode_4032x3040_30fps` table: RAW10, 2-lane, line length
  9024, frame length 3102, IOP PLL 24MHz/3*262 = 2096MHz VCO with IOPSYCK
  div 1 -> 2096 Mbps/lane -> **1048 MHz link frequency**; full MIPI
  global-timing register set for that rate (0x080a..0x0819,
  0xe04c..0xe04f)

## Modes (all 2-lane)

| Mode | Format | HTS | VTS def | Max fps | Link freq |
|---|---|---|---|---|---|
| 4056x3040 | SRGGB12 | 24000 | 3500 | 10 | 450 MHz |
| 2028x1520 (2x2 bin) | SRGGB12 | 12740 | 1648 | 40 | 450 MHz |
| 1332x990 (bin+crop) | SRGGB10 | 6664 | 1050 | 120 | 450 MHz |
| 3840x2160 (crop) | SRGGB10 | 9024 | 3102 | 30 | 1048 MHz |

The RPi 2028x1080 50fps crop mode was left out (easy to add later: another
table + mode entry).

### 3840x2160@30 mode provenance (copied vs derived)

Copied verbatim from nv_imx477's validated `imx477_mode_4032x3040_30fps`
(which reads MORE rows/frame than 4K at the same link rate, so the rate is
silicon-proven): all PLL/link registers, line length 9024, frame length
3102, MIPI global timing, readout config, plus the 10-bit support regs
(0x420b..0x9a4d) that nv_imx477 keeps in its common table (prepended to
the mode table here, exactly as the RPi driver does for its 10-bit mode).

Derived (only the window):
- analog crop Y = rows 440..2599 (0x0346/47=0x01b8, 0x034a/4b=0x0a27) —
  cross-validated: the RPi driver's own 2028x1080 mode uses these exact
  values for the same centred 2160-row window
- digital crop X offset 108 = (4056-3840)/2 (0x0409=0x6c), Y offset 0
- crop/output size 3840x2160 (0x040c..0x040f, 0x034c..0x034f)

One `TODO(compile-check)` left in the table: 0x3f56/0x3f57 (per-mode Sony
readout/powersave timing) reused from the 4032x3040 table since the value
for a 2160-row window is unpublished; verify on first stream test.

## Deliberate deviations from the references

1. **PIXEL_RATE = 840 MHz (sensor internal clock), not the Rockchip
   link-based formula.** HTS/VTS are in 840MHz units (RPi
   `line_length_pix`), so `fps = pixel_rate/(hts*vts)` is exact this way.
   The Rockchip formula (`link_freq*2*lanes/bpp` = 150M/180M) would make
   rkaiq's exposure-time math wrong by ~5x. Link frequency (used by the
   CSI2 DPHY) is reported separately and correctly via V4L2_CID_LINK_FREQ.
2. **`rockchip,camera-module-*` DT properties are optional** (imx577 probe
   hard-fails without them). Defaults: index 0, facing "back", module
   "RPI-HQ", lens "default".
3. **xvclk is optional** (`devm_clk_get_optional`): the HQ camera has its
   own on-board 24MHz oscillator. When a clock IS given (fixed-clock or SoC
   output) it is rate-set/enabled the Rockchip way, warning on mismatch.
4. **Trigger mode from DT string** `trigger-mode = "source" | "sink"`
   (absent = free running), applied at stream-on after the mode table,
   before 0x0100=1. Register values per role (validated on Jetson):

   | role | 0x3F0B MC_MODE | 0x3041 MS_SEL | 0x3040 XVS_IO_CTRL | 0x4B81 EXTOUT_EN |
   |---|---|---|---|---|
   | none | 0 | 1 | 0 | 0 |
   | source | 1 | 1 | 1 | 1 |
   | sink | 1 | 0 | 0 | 0 |

   Module param `trigger_mode=1|2` kept as a fallback when the DT property
   is absent. Each sensor node gets exactly one role — no both-roles path.
5. **Common regs written once per power-on** (RPi's `common_regs_written`
   flag, cleared in power_off) instead of unconditionally in `s_power` —
   works both through the rkaiq pipeline (s_power then s_stream) and plain
   v4l2 (s_stream only). The imx577's HDR/preisp paths were dropped (no HDR
   modes on IMX477); GET/SET_HDR_CFG still answer NO_HDR for rkaiq.
6. **RAW-format regs (0x0112/0x0113/0x0114) prepended to the two 12-bit
   mode tables** — the RPi tables rely on the common table for these, which
   breaks when switching away from the 10-bit 1332x990 mode without a power
   cycle.
7. hflip/vflip and the test-pattern RGB component controls were dropped
   (flips change the Bayer order, which the rkaiq IQ matching doesn't
   handle); Bayer order is fixed SRGGB.
8. **Analog gain follows the Rockchip vendor contract, not the RPi one**:
   V4L2_CID_ANALOGUE_GAIN takes linear gain in 1/16 units (range 16..356,
   step 1, default 32 = 2.0x) and the driver converts internally with the
   imx577 reference's exact formula `reg = 1024 - 1024 * 16 / ctrl_val`
   (integer division), clamped to the IMX477 register ceiling 978
   (~22.26x; ctrl 356 -> reg 978). This is what rkaiq's AE and the
   translated IQ file's sensor_calib gain model assume (the IQ
   TRANSLATION_NOTES flag it as checklist item #1). Unlike the imx577,
   gains above the analog ceiling do NOT spill into digital gain here —
   digital gain stays a separate V4L2_CID_DIGITAL_GAIN control (0x020e,
   8.8 fixed point, RPi convention).

## TODO(compile-check) / risks

No `/* TODO(compile-check) */` markers were left in the code; the genuinely
uncertain spots are listed here instead:
- `RKMODULE_GET_CHANNEL_INFO` / `struct rkmodule_channel_info` and
  `RKMODULE_SET_QUICK_STREAM` must exist in this kernel's
  `linux/rk-camera-module.h` (they do in rk356x/rk3588 6.1 vendor trees; if
  the build errors, delete those two ioctl cases).
- probe signature is the old two-argument `(client, id)` style, matching
  the imx577 reference on this vendor 6.1 tree. If the tree has the
  probe_new conversion applied to i2c, switch to `.probe_new`/one-arg.
- `v4l2_subdev_get_try_format` naming matches the vendor 6.1 reference; a
  newer media stack would want `v4l2_subdev_state_get_format`.
- `vblank_max` uses the RPi long-exposure headroom
  `(128 * 0xffdc) - height`; if rkaiq misbehaves with such a large range,
  clamp to `0xffdc - height`.
- CROP_BOUNDS for the 2028/1332 modes returns the full mode size (imx577
  behaviour); if rkisp complains about 16-pixel alignment on 2028-wide
  modes, mirror the 4048 trick there (3840 and 2160 are already aligned).
- **4K mode link rate**: 2096 Mbps/lane is above D-PHY v1.2 (1.5G) but
  within both the IMX477 (2.1G/lane max) and the RK3588 D-PHY RX (2.5G)
  limits. Risks for first stream test: the RK3588 CSI2/DPHY driver must
  pick up the 1048MHz entry from V4L2_CID_LINK_FREQ (the driver switches
  the control per-mode in set_fmt, imx577-style); flex/cable signal
  integrity at 2.1G-class rates; and nv_imx477's odd 0x0820/0x0821 value
  (0x20c0 = 8384 "Mbps" vs the expected 4192 total) was copied verbatim
  since it streamed fine on Jetson.

## Build / install on the ROCK 5T

```sh
sudo apt install linux-headers-$(uname -r)   # if not present
cd ~/orin-recorder/rock5t-camera/driver      # after pulling the repo
make
sudo make install                            # copies to /lib/modules/.../extra + depmod
sudo modprobe imx477                         # or: sudo insmod ./imx477.ko
dmesg | grep imx477                          # expect "Detected Sony imx0477 sensor"
```

DT sensor node sketch (per camera, on its i2c bus, addr 0x1a):

```dts
imx477: imx477@1a {
	compatible = "sony,imx477";
	reg = <0x1a>;
	/* clocks optional - module has on-board 24MHz osc */
	avdd-supply = <&vcc_cam_avdd>;   /* or fixed dummies */
	dovdd-supply = <&vcc_cam_dovdd>;
	dvdd-supply = <&vcc_cam_dvdd>;
	reset-gpios = <&gpio... GPIO_ACTIVE_HIGH>;  /* XCLR */
	rockchip,camera-module-index = <0>;
	rockchip,camera-module-facing = "back";
	rockchip,camera-module-name = "RPI-HQ";
	rockchip,camera-module-lens-name = "default";
	trigger-mode = "source";        /* other sensor: "sink" */
	port {
		imx477_out: endpoint {
			remote-endpoint = <&mipi_in_cam0>;
			data-lanes = <1 2>;
		};
	};
};
```
