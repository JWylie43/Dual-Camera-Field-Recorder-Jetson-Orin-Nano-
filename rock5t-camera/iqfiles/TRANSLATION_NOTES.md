# imx477_RPI-HQ_default.json — translation notes

**Status: DRAFT — structurally valid, never loaded on hardware.**

Target: RK3588 (Rock 5T), rkisp v30 schema, rkaiq 6.8.0.
Filename follows Radxa's `<sensor>_<module>_<lens>.json` convention: sensor `imx477`,
module `RPI-HQ`, lens `default`.

## Sources

| File | Role |
|---|---|
| `reference/imx577_radxa_12m_iq.json` | **Primary skeleton.** Radxa's shipping RK3588 IQ for the Sony IMX577 — the IMX477's near-identical sibling (same 12.3MP class, 1.55µm pixel, same Sony CFA family, same hyperbolic gain law). Everything not listed below is KEPT from it verbatim. |
| `reference/imx477_rpi_tuning.json` | Raspberry Pi's published lab calibration for the IMX477 (libcamera format). Source of all measured numbers that were overridden. |
| `reference/imx415_radxa_iq_skeleton.json` | Secondary structural reference only (used to confirm schema conventions, e.g. the gamma X-grid; no numbers taken from it). |
| `reference/imx577_rockchip_driver.c` | Confirms the gain contract (see sensor_calib). |

The skeleton was switched from the imx415 file to the imx577 file mid-work: the
imx577 module shares the IMX477's pixel and gain architecture, so its noise/NR,
BLC and sensor sections are already approximately right and could be **kept**
instead of translated. Two cross-checks validated the switch:

- **BLC cross-validation:** the imx577 file's measured black level is 254.7–257
  (12-bit); RPi's imx477 value is 4096 on a 16-bit scale = exactly **256** at
  12-bit. Independent calibrations agreeing to ~1 LSB.
- **Gain law:** `imx577_rockchip_driver.c` line ~2054 converts V4L2 linear gain to
  `gain_reg = 1024 − 1024/gain` — the identical formula the IMX477 datasheet/RPi
  driver uses.

Generator script (reproducible): the JSON was produced by
`rock5t-camera/iqfiles/gen_imx477_iq.py` (run it from anywhere; paths are
absolute-from-repo). Every transform below matches its printed output — edit
the script and regenerate rather than hand-editing the JSON.

---

## Section-by-section

### sensor_calib — mostly KEPT, 2 edits
- `resolution`: 4048x3040 → **4056x3040** (IMX477 full active array, as the RPi
  kernel driver reports). *If the ported Rockchip imx477 driver crops to a
  different mode (e.g. 4048 like the imx577 one), change this to match — and add
  a matching `lsc_v2` resolution entry.*
- `CISTimeSet.Linear.CISTimeRegMin`: 1 → **4** (RPi imx477 kernel driver:
  minimum coarse integration = 4 lines).
- `Gain2Reg` KEPT: `EXPGAIN_MODE_LINEAR`, `GainRange [1, 16, 32, 0, 1, 32, 512]`
  (linear gain, 1–16x, encoded ×32). The Rockchip imx577 driver takes linear
  V4L2 gain and does the Sony hyperbolic register conversion internally
  (`1024 − 1024·16/val`, with driver-side dgain handover above ~22x). A ported
  imx477 driver must keep the same contract; then this section is correct as-is.
  **This is the #1 thing to verify once a driver exists** — a mismatch shows up
  as AE oscillation / brightness steps when gain changes.
- `CISLinTimeRegMaxFac [1, 22]` KEPT — matches IMX477 too (max exposure =
  VTS − 22 lines in the RPi driver).

### module_calib — KEPT
`FNumber 1.6 / EFL 5.2` are the Radxa 12M module's optics, not ours (CS-mount
lens on the HQ camera, lens TBD). Only used for the (disabled) EnvLv calibration
and metadata; update when the rig's lens is chosen.

### ablc_calib (black level) — KEPT
Skeleton values ~254.7–257 per channel per ISO (12-bit domain).
Scale inference: RPi gives 4096 on 16-bit → 4096/16 = **256 at 12-bit**; the
skeleton's magnitudes (~256, vs Sony 12-bit pedestal class) confirm the section
is in 12-bit units. Since Radxa's measured imx577 values equal RPi's imx477
value to ≈1 LSB, the measured (slightly per-ISO-varying) values were kept rather
than flattened to a constant 256.

### wb_v21 (AWB) — gains replaced, detection geometry KEPT
Computed from RPi `rpi.awb.ct_curve` — entries `(CT, r, b)` with `r = R/G`,
`b = B/G` of grey under that illuminant; gain = 1/r, 1/b; linear interpolation
in CT (same as libcamera):

| Illuminant | CT used | r, b (interp) | Rgain | Bgain |
|---|---|---|---|---|
| HZ  | 2350 | 0.6009, 0.3093 | 1.66417 | 3.23311 |
| A   | 2856 | 0.5047, 0.4057 | 1.98143 | 2.46486 |
| TL84| 4000 | 0.3983, 0.6036 | 2.51090 | 1.65668 |
| CWF | 4150 | 0.3880, 0.6210 | 2.57763 | 1.61037 |
| D50 | 5000 | 0.3417, 0.6874 | 2.92631 | 1.45476 |
| D65 | 6500 | 0.3085, 0.7169 | 3.24144 | 1.39496 |
| D75 | 7500 | 0.2981, 0.7352 | 3.35477 | 1.36021 |

Applied to:
- `lightSources[*].standardGainValue` (the skeleton has 5 sources: A, CWF, D65,
  HZ, TL84 — no D50/D75 entries exist in this AWB section).
- `lightSources[*].defaultDayGainLow` = D50 gains, `defaultDayGainHigh` = D65
  gains (skeleton convention: low-LV / high-LV daylight fallback).
- `manualPara.cfg.mwbGain` = D50 gains (its scene tag is DAYLIGHT/CCT 5000).

**KEPT (not translatable without hardware):** all white-point *detection*
geometry — `uvRegion`, `xyRegion`, `rtYuvRegion`, `rgb2TcsPara`,
`rgb2RotationYuvMat`, weights. These are measured in rkisp statistics space for
the imx577 module.

**Known consequence (deliberate):** I partially reverse-engineered the xy space
(x ≈ `−0.618·log2(r) + 0.786·log2(b) + 1.440` reproduces the skeleton's region
x-ranges for its own illuminants). Under that transform the IMX477's daylight
white point lands at x ≈ 2.11 — *outside* every skeleton region (max ≈ 1.92).
So outdoors the AWB will likely classify no illuminant and fall back to
`defaultDayGainLow/High`, which are set to the correct measured IMX477 D50/D65
gains. For an outdoor-only draft this is an acceptable, predictable behavior;
proper region recalibration needs the RKISP tuning tool with the real camera.
The y-axis formula could not be recovered, which is why regions were not
shifted numerically.

Note the large gain difference vs the skeleton (imx577 D65 R≈1.94 vs imx477
D65 R≈3.24): plausibly the Radxa module's IR-cut filter; RPi's numbers are for
exactly our sensor + HQ-camera IR filter stack, so they were preferred.
`sensitivity_r = sensitivity_b = 1.05` from the RPi file was **not** applied
(module-to-module trim; ±5% uncertainty on all gains above).

### ccm_calib — matrices + awbGain replaced, structure KEPT
- `aCcmCof[*].awbGain` (7 illuminants, incl. D50/D75) → the RPi-derived
  [Rgain, Bgain] pairs above. These drive CCM illuminant selection by distance
  from the applied WB gain, so they must match the AWB numbers.
- `matrixAll[*].ccMatrix`: RPi `rpi.ccm` matrices (cts 2850/2960/3580/4559/
  5881/7600), **linearly interpolated in CT** to each skeleton illuminant CT
  (HZ clamped to the 2850 matrix). Convention check: RPi rows sum to 1.0,
  identical to the skeleton's row convention → **no re-normalization or
  reordering needed** (rows re-normalized to sum exactly 1 after interpolation
  anyway; offsets 0).
- Saturation-74 variants: RPi has no desaturated set, so
  `M_74 = 0.74·M + 0.26·L` where every row of `L` is the luma vector
  `rgb2y_para/128 = [0.2969, 0.5859, 0.1172]` (standard "blend toward luma"
  desaturation; rows still sum to 1). **Approximation** — but this skeleton's
  `gain_sat_curve` pins saturation at 100 for all gains, so the _74 matrices are
  structural only and never selected at runtime.
- `lumaCCM`, `illu_estim`, tolerances: KEPT.

### agamma_calib_v11 — replaced with RPi tone curve
- Skeleton X grid recovered and verified: the 49 `Gamma_curve` samples sit on
  the fixed rkisp grid `[0,1,2,…,8,10,12,…,4095]` (dyadic segments); the
  skeleton's own values reproduce `x^(1/2.2)` on that grid to ±0.5 LSB, which
  confirms both the grid and the 12-bit domain.
- Conversion: for each grid X, input16 = X·65535/4095 → piecewise-linear lookup
  of RPi `rpi.contrast.gamma_curve` (16-bit in/out pairs) → output·4095/65535,
  rounded; endpoint forced to 4095; monotonicity asserted.
- **Character note:** RPi's curve is piecewise-linear starting (0,0)→(1024,5040)
  (16-bit), so deep shadows are lifted far less than the skeleton's pure 2.2
  curve (first sample 5 vs 93), while mids are slightly brighter (25% grey →
  2540 vs 2181/4095). That is genuinely the RPi HQ rendering; if shadows look
  crushed on hardware, this section is the knob.

### lut3d_calib — KEPT (disabled)
Already `enable: 0` in the skeleton. Left off: the RPi pipeline has no 3D LUT,
its CCM is the entire color mapping.

### lsc_v2 (lens shading) — neutralized (deliberate)
- RPi `rpi.alsc` tables were **not transplanted** (they correct the RPi stock
  lens, not the rig's CS-mount lens); the skeleton's tables correct the Radxa
  12M module lens — also wrong. All 33 skeleton tables (17×17 grid, all four
  channels) set to **1024 = 1.0x** (unity; skeleton's own minimum coefficient
  is 1024, confirming the fixed-point scale).
- LSC left `enable: 1` with unity tables — guaranteed no-op, structurally
  identical to a shipping file.
- Added resolutions `4056x3040` (full frame) and `2028x1520` (2×2 binned) with
  unity tables cloned per illuminant (sector sizes: even values summing to the
  frame — 12×254+4×252 / 16×190, and 6×128+10×126 / 8×96+8×94), placed first in
  `resolutionAll`. Original 4048x3040 / 3840x2160 / 1920x1080 entries kept.
  Table count 33 → 55.
- `alscCoef.illAll[*].wbGain` updated to the RPi-derived gains (GRAY kept [1,1])
  so illuminant selection stays consistent, even though all tables are unity.
- Real LSC for the actual lens must be calibrated on hardware (flat-field, per
  CT); until then expect corner falloff and possible slight color vignetting.

### ae_calib — KEPT
Route caps at 64x total gain via the driver's combined-gain handling (the
imx577 driver folds >22x into sensor dgain transparently; a ported imx477
driver should do the same). Time route tops at 30ms (30fps). Setpoints,
antiflicker (50Hz — change to 60Hz for US venues if relevant), damping: all
Radxa's. RPi AGC content (metering weights, y_targets) was **not** ported —
different algorithm structure, unclear mapping.

### Noise reduction (bayer2dnr_v2, bayertnr_v2, ynr_v3, cnr_v2, sharp_v4, gain_v2) — KEPT
Calibrated for the imx577's identical 1.55µm pixel — the best available match.
RPi's noise model (`rpi.noise`: stddev ≈ 2.767·√level, 16-bit domain) does not
map cleanly onto rkaiq's per-ISO, per-frequency sigma tables, so no scaling was
attempted. Keep the RPi constants on file as ground truth for later hardware
tuning.

### Everything else — KEPT verbatim
`adegamma` (off), `agic`, `debayer`, `amerge`, `adrc`, `adehaze`, `adpcc`
(defect pixels — imx577-measured but same pixel class), `aldch` (off), `cpsl`,
`cproc`, `ie`, `colorAsGrey`, `cac_v03/v10`, `af_v30` (the rig has a manual CS
lens — AF config is inert without a VCM; disable later if it logs errors),
`afec` (off), `csm`, `cgc`, top-level `uapi`, `sys_static_cfg`, scene markers
(`main_scene[0] "normal"` / `sub_scene[0] "day"`).

Output serialized in the same cJSON style as the shipping file (tab indent,
inline scalar arrays); validated with `python3 -m json.tool` and byte-equal
round-trip reparse.

---

## Hardware validation checklist (first light, in order)

1. **File loads:** rkaiq starts without parse/calib errors with the file in
   `/etc/iqfiles/` (name must match what the driver's module info reports —
   sensor `imx477`, module `RPI-HQ`, lens `default`). If rkaiq rejects it,
   compare against `imx577_radxa_12m_iq.json` loading on the same board first.
2. **Exposure/gain sanity (top risk):** point at a static scene, watch AE
   settle. Oscillation, stair-stepping, or brightness jumps when gain crosses
   values ⇒ the driver's gain contract doesn't match `Gain2Reg` (see
   sensor_calib note). Verify the ported imx477 driver interprets
   V4L2 analogue gain as linear ×16 units and applies `1024−1024/g` itself.
3. **Black level:** cap the lens; histogram of a raw-ish dark capture should
   sit at ~0 after ISP (no purple/green shadows, no crushed floor). Wrong BLC
   shows as milky or tinted shadows. Expected pedestal 256@12bit.
4. **Grey neutrality outdoors (the priority):** grey card / concrete / white
   lines in full sun and in shade. Expect the AWB to run on the daylight
   fallback gains (see wb_v21 note) — verify WB gains reported by rkaiq land
   near R≈2.9–3.3, B≈1.36–1.46. A strong cast means the fallback isn't being
   hit as predicted; check rkaiq AWB logs for which light source it claims.
5. **Skin / grass / sky sanity:** grass should be green not teal (CCM row 2),
   sky blue not cyan (row 3), skin not orange (row 1 + CT interp). If daylight
   color is off but grey is neutral, suspect the CCM CT-interpolation choices
   or the AWB→CCM `awbGain` matching.
6. **Shadow rendering:** if shadows look crushed vs the Pi's output, revisit
   the gamma section (RPi curve has a gentle toe).
7. **Lens shading:** expect visible corner falloff (LSC is unity). Calibrate
   real LSC tables for the chosen CS lens with the Rockchip tuning tool, per CT.
8. **Resolution match:** confirm the driver's active mode is 4056x3040 (or fix
   `sensor_calib.resolution` + add the LSC resolution entry to match).
9. **Antiflicker:** venue lighting at 60Hz mains ⇒ switch
   `AecAntiFlicker.Frequency` to `AECV2_FLICKER_FREQUENCY_60HZ`.

## Biggest uncertainties (ranked)

1. **Gain2Reg vs the (not yet existing) Rockchip imx477 driver** — AE correctness
   hinges on the driver mirroring the imx577 driver's linear-gain contract.
2. **AWB detection regions are imx577-module measurements** — daylight expected
   to ride the fallback gains; indoor/mixed light will misbehave. Needs real
   calibration.
3. **CCM CT-interpolation** — RPi matrices were measured at 6 CTs and linearly
   interpolated to the skeleton's 7 illuminant CTs; daylight range (4559–7600)
   is well covered, deep warm (HZ 2350) is clamped/extrapolated.
4. **NR tuned for imx577 module** — same pixel, different lens/ISP gains; high
   ISO texture/chroma noise balance unverified.
5. **LSC unity** — cosmetic until calibrated (corner falloff, possible color
   shading with wide CS lenses).
