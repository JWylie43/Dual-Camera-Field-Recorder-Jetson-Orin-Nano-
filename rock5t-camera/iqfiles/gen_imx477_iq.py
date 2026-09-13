#!/usr/bin/env python3
"""Generate rock5t-camera/iqfiles/imx477_RPI-HQ_default.json (rkisp v30, rkaiq 6.8.0).

Primary skeleton : reference/imx577_radxa_12m_iq.json  (Radxa shipping IQ for the
                   IMX577 -- the IMX477's near-identical sibling; most sections KEPT)
Numbers source   : reference/imx477_rpi_tuning.json    (Raspberry Pi lab calibration)
Secondary ref    : reference/imx415_radxa_iq_skeleton.json (structure cross-check only)

Every transform prints its math so TRANSLATION_NOTES.md stays traceable.
"""
import json, copy, math

BASE = '/Users/josephwylie/Desktop/orin-nano-recorder/rock5t-camera'
skel = json.load(open(BASE + '/reference/imx577_radxa_12m_iq.json'))
rpi_raw = json.load(open(BASE + '/reference/imx477_rpi_tuning.json'))
rpi = {}
for a in rpi_raw['algorithms']:
    rpi.update(a)

out = copy.deepcopy(skel)
isp = out['main_scene'][0]['sub_scene'][0]['scene_isp30']

def interp(x, xs, ys):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])

def r6(v):
    return round(v, 6)

# ================================================================ 1. AWB gains
# RPi ct_curve entries are (CT, r, b) with r = R/G, b = B/G of grey under that
# illuminant as seen by the IMX477 + HQ-camera IR filter.  WB gain = 1/r, 1/b.
# Linear interpolation in CT (libcamera does the same on this curve).
cc = rpi['rpi.awb']['ct_curve']
cts = cc[0::3]; rs = cc[1::3]; bs = cc[2::3]

ILLUM_CT = {'HZ': 2350, 'A': 2856, 'TL84': 4000, 'CWF': 4150,
            'D50': 5000, 'D65': 6500, 'D75': 7500}

gains = {}
for name, ct in sorted(ILLUM_CT.items(), key=lambda kv: kv[1]):
    r = interp(ct, cts, rs)
    b = interp(ct, cts, bs)
    gains[name] = (r6(1.0 / r), r6(1.0 / b))
    print(f'AWB {name:5s} CT={ct}: r={r:.4f} b={b:.4f} -> Rgain={1/r:.5f} Bgain={1/b:.5f}')

D50g, D65g = gains['D50'], gains['D65']

wb = isp['wb_v21']
# MANUAL white balance is the shipped default (validated 2026-09-11: manual
# D50 gains produce correct color; auto mode misclassifies because the
# detection zones are imx577-module values). Manual is also the RIGHT design
# for this rig: both cameras get identical locked WB, so the stitched pano
# has no left/right color seam, and the use case is outdoor daylight sports.
# Set WB_MODE = 'auto' only for future zone-recalibration experiments.
# The imx577 skeleton ships Evbias = -1.2 (a >1-stop deliberate underexposure
# inherited by accident, discovered 2026-09-11 — footage ran dark). Neutral 0
# is the sane baseline; revisit against real outdoor footage (a mild negative
# bias MAY be wanted for highlight protection in sports, but choose it, don't
# inherit it). AecGridWeight (metering zones) and BackLightCtrl exist in the
# skeleton for later tuning; left untouched — for the stitched pano, both
# cameras should meter identically (cam0 is the single AE brain anyway).
isp['ae_calib']['LinearAeCtrl']['Evbias'] = 0.0
print('AE Evbias: -1.2 (skeleton) -> 0.0')

WB_MODE = 'manual'
wb['control']['mode'] = ('CALIB_WB_MODE_MANUAL' if WB_MODE == 'manual'
                         else 'CALIB_WB_MODE_AUTO')
print(f'AWB control mode: {wb["control"]["mode"]}')
# manual gains (scene DAYLIGHT, CCT 5000 in the skeleton) -> RPi D50
wb['manualPara']['cfg']['mwbGain'] = [D50g[0], 1, 1, D50g[1]]
for ls in wb['autoPara']['lightSources']:
    g = gains[ls['name']]
    ls['standardGainValue'] = [g[0], 1, 1, g[1]]
    # daylight fallback gains (used when stats land outside all detection
    # regions -- expected here, see notes): Low LV -> D50, High LV -> D65
    ls['defaultDayGainLow'] = [D50g[0], 1, 1, D50g[1]]
    ls['defaultDayGainHigh'] = [D65g[0], 1, 1, D65g[1]]
print('AWB: standardGainValue/defaultDayGain* replaced; detection regions KEPT (imx577)')

# ================================================================ 2. CCM
# RPi CCMs: 3x3 row-major, rows sum to 1.0 -- same convention as the skeleton
# matrices.  Interpolated linearly in CT onto the skeleton's illuminant set.
# sat-100 = RPi matrix; sat-74 = blend toward the luma projection:
#   M_s = s*M + (1-s) * (each row := rgb2y_para/128), rows still sum to 1.
# (gain_sat_curve in this skeleton pins saturation at 100, so the _74 entries
# are structural only.)
rccm = rpi['rpi.ccm']['ccms']
ccm_cts = [e['ct'] for e in rccm]
ccm_mats = [e['ccm'] for e in rccm]

def ccm_at(ct):
    if ct <= ccm_cts[0]:
        return list(ccm_mats[0])
    if ct >= ccm_cts[-1]:
        return list(ccm_mats[-1])
    for i in range(len(ccm_cts) - 1):
        if ccm_cts[i] <= ct <= ccm_cts[i + 1]:
            t = (ct - ccm_cts[i]) / (ccm_cts[i + 1] - ccm_cts[i])
            return [a + t * (b - a) for a, b in zip(ccm_mats[i], ccm_mats[i + 1])]

LUMA = [38 / 128, 75 / 128, 15 / 128]  # skeleton lumaCCM rgb2y_para / 128

def desat(m, s):
    return [s * m[r * 3 + c] + (1 - s) * LUMA[c] for r in range(3) for c in range(3)]

def rownorm(m):
    o = []
    for r in range(3):
        rsum = sum(m[r * 3:r * 3 + 3])
        o += [r6(m[r * 3 + c] / rsum) for c in range(3)]
    return o

ccm = isp['ccm_calib']['TuningPara']
for cof in ccm['aCcmCof']:
    g = gains[cof['name']]
    cof['awbGain'] = [g[0], g[1]]
for mat in ccm['matrixAll']:
    m = ccm_at(ILLUM_CT[mat['illumination']])
    if mat['saturation'] != 100:
        m = desat(m, mat['saturation'] / 100.0)
    mat['ccMatrix'] = rownorm(m)
    mat['ccOffsets'] = [0, 0, 0]
    print(f"CCM {mat['name']:9s} (CT {ILLUM_CT[mat['illumination']]}, sat {mat['saturation']}): "
          + ' '.join(f'{v:+.4f}' for v in mat['ccMatrix']))

# ================================================================ 3. BLC -- KEPT
# Skeleton (imx577, measured): ~254.7..257 per channel at 12 bit.
# RPi imx477: black_level 4096 on 16-bit scale = 4096/16 = 256 at 12 bit.
# Identical pedestal -> keep Radxa's measured per-ISO values unchanged.
blc = isp['ablc_calib']['BlcTuningPara']['BLC_Data']
print('BLC kept: R@ISO50 =', blc['R_Channel'][0], '(RPi imx477 expectation: 256.0)')

# ================================================================ 4. Gamma
# Skeleton Gamma_curve = 49 samples on the fixed rkisp v30 X grid below,
# 12-bit domain (verified: skeleton values == x^(1/2.2) on this grid, +-0.5).
# Resample RPi rpi.contrast gamma_curve (16-bit in/out point pairs) onto it.
XGRID = [0,1,2,3,4,5,6,7,8,10,12,14,16,20,24,28,32,40,48,56,64,80,96,112,128,
         160,192,224,256,320,384,448,512,640,768,896,1024,1280,1536,1792,2048,
         2304,2560,2816,3072,3328,3584,3840,4095]
gc = rpi['rpi.contrast']['gamma_curve']
gx = gc[0::2]; gy = gc[1::2]
curve = [int(round(interp(x * 65535.0 / 4095.0, gx, gy) * 4095.0 / 65535.0)) for x in XGRID]
curve[-1] = 4095
assert all(b >= a for a, b in zip(curve, curve[1:])), 'gamma must be monotonic'
isp['agamma_calib_v11']['GammaTuningPara']['Gamma_curve'] = curve
print('Gamma (RPi contrast) first 10:', curve[:10], 'last 5:', curve[-5:])

# ================================================================ 5. 3D LUT -- KEPT
# Already disabled in the imx577 skeleton (common.enable = 0).  Left disabled:
# the RPi pipeline has no 3D LUT; CCM carries the whole colour mapping.
assert isp['lut3d_calib']['common']['enable'] == 0

# ================================================================ 6. LSC
# Radxa tables correct the 12M module's own lens; RPi alsc tables correct the
# RPi stock lens.  Neither matches the rig's CS-mount lens -> neutralise all
# tables to unity (1024 = 1.0x) and add IMX477-native resolutions.
lsc = isp['lsc_v2']
for t in lsc['tbl']['tableAll']:
    for ch in ('lsc_samples_red', 'lsc_samples_greenR', 'lsc_samples_greenB', 'lsc_samples_blue'):
        n = len(t[ch]['uCoeff'])
        assert n == 289, n
        t[ch]['uCoeff'] = [1024] * n
for ill in lsc['alscCoef']['illAll']:
    if ill['name'] in gains:
        g = gains[ill['name']]
        ill['wbGain'] = [g[0], g[1]]   # GRAY entry left as [1, 1]

def mk_res(name, sx, sy):
    return {'name': name, 'lsc_sect_size_x': sx, 'lsc_sect_size_y': sy}

res_new = [
    mk_res('4056x3040', [254] * 12 + [252] * 4, [190] * 16),   # imx477 full frame
    mk_res('2028x1520', [128] * 6 + [126] * 10, [96] * 8 + [94] * 8),  # 2x2 binned
]
for r in res_new:
    assert sum(r['lsc_sect_size_x']) == int(r['name'].split('x')[0])
    assert sum(r['lsc_sect_size_y']) == int(r['name'].split('x')[1])
    assert all(v % 2 == 0 for v in r['lsc_sect_size_x'] + r['lsc_sect_size_y'])
lsc['common']['resolutionAll'] = res_new + lsc['common']['resolutionAll']
lsc['common']['resolutionAll_len'] = len(lsc['common']['resolutionAll'])

donor = [t for t in lsc['tbl']['tableAll'] if t['resolution'] == '4048x3040']
for r in res_new:
    for t in donor:
        nt = copy.deepcopy(t)
        nt['resolution'] = r['name']
        nt['name'] = t['name'].replace('4048x3040', r['name'])
        lsc['tbl']['tableAll'].append(nt)
lsc['tbl']['tableAll_len'] = len(lsc['tbl']['tableAll'])
print('LSC: all tables unity; resolutions:',
      [r['name'] for r in lsc['common']['resolutionAll']],
      'tables:', lsc['tbl']['tableAll_len'])

# ================================================================ 7. sensor_calib
# KEPT almost verbatim: the Rockchip imx577 driver takes V4L2 analogue gain as
# LINEAR gain (1/16 units) and converts to the Sony hyperbolic register
# (1024 - 1024/gain) internally -- the imx477 uses the same formula, so a
# ported driver keeps the same Gain2Reg contract.  Changes:
sc = out['sensor_calib']
sc['resolution'] = {'width': 4056, 'height': 3040}       # imx477 full active array
sc['CISTimeSet']['Linear']['CISTimeRegMin'] = 4          # imx477 kernel: min 4 lines
print('sensor_calib: resolution 4056x3040, CISTimeRegMin 4; gain model kept (linear 1..16x)')

# ================================================================ 8. AE route (daylight rig)
# The rig records outdoors only, and analog gain adds noise without adding
# light.  Strategy: the shutter does ALL the work up to the frame limit
# (33ms @30fps); analog gain stays pinned at 1.0 across the whole shutter
# range and only rises to 2.0 as an absolute last resort at full shutter
# of black).  ISP digital gain is never used.
#
# SPORTS BLUR CEILING: motion blur is the quality metric for game footage,
# so the shutter is CAPPED AT 8ms -- past that, AE prefers gain 2.0 over
# more blur, and only once gain is exhausted may the shutter extend to the
# 33ms frame limit (truly dark scenes: degrade to blurry-but-visible).
# Adaptation budget: sun ~1ms -> 8ms (3 stops) -> gain 2 (1 stop) = 4 stops,
# covering sun-to-overcast with blur bounded and noise capped at gain 2.
# The 8ms number is a taste call -- revisit with real game clips at bring-up.
ae_route = isp['ae_calib']['LinearAeCtrl']['Route']
ae_route['TimeDot'] = [0, 0.0005, 0.002, 0.008, 0.008, 0.033]
ae_route['TimeDot_len'] = 6
ae_route['GainDot'] = [1, 1, 1, 1, 2, 2]
ae_route['GainDot_len'] = 6
ae_route['IspDGainDot'] = [1, 1, 1, 1, 1, 1]
ae_route['IspDGainDot_len'] = 6
print('AE route: daylight sports strategy -- shutter capped 8ms, then gain '
      'to 2.0, 33ms only as last resort; ISP dgain off')

# ============================================ 8b. AE brightness setpoint raise
# Midday measurement (2026-09-13): the imx577 skeleton's DySetpoint sits at
# 16-20% (and DROPS for bright scenes) -> sunny frames average ~44/255 with
# only 0.25% highlight clip. That's a stop of unused headroom, and lifting
# 8-bit H.265 in post costs quality. Raise the target ~0.7EV; verified against
# clip stats on re-shot frames (target: mean ~70-80/255, clip < ~2%).
_ds = isp['ae_calib']['LinearAeCtrl']['DySetpoint']
_old = list(_ds['DySetpoint'])
_new = [30, 30, 29, 28, 27, 26]
_ds['DySetpoint'] = _new
print('AE DySetpoint:', _old, '->', _new, '(~+0.7EV daylight)')

# ======================================== 8c. AE metering grid (field-weighted)
# Field measurement (2026-09-13, real pitch): with sky in frame the flat
# 15x15 grid lets the sky drag the whole-frame average down — ground pinned
# at 42/255 while the sky idles at 117 with 0.4% clip. Game film cares about
# the FIELD; sky detail is worthless. Weight: field rows x4, horizon x2,
# sky rows x1 (sky still counts a little, so it doesn't blow out wildly).
# Exposure stays single-brain across cameras (cam0 AE, ae_follower mirrors).
#
# ORIENTATION: grid rows are sensor rows. Cameras are currently mounted
# UPSIDE DOWN (scene sky lands on sensor-bottom rows). After the re-mount,
# flip this to 'upright' and regenerate.
SENSOR_ORIENTATION = 'inverted'   # 'upright' once cameras are re-mounted

_rows15 = ([1]*5 + [2]*2 + [4]*8)          # upright: sky top, field bottom
if SENSOR_ORIENTATION == 'inverted':
    _rows15 = _rows15[::-1]
_grid = []
for _w in _rows15:
    _grid += [_w]*15
isp['ae_calib']['CommCtrl']['AecGridWeight'] = _grid
print('AE grid: field-weighted (sky x1 / horizon x2 / field x4),',
      SENSOR_ORIENTATION)

# ================================================= 9. texture tuning (round 1)
# First-light 1:1 crops (2026-09-13, outdoor daylight, base ISO): grass texture
# smeared to watercolor by spatial NR that has nothing to denoise in full sun,
# and hard sharpening halos on car edges (imx577-inherited sharp_ratio = 15!).
# Round 1: calm the LOW-ISO rows only (<= 400 tapered; real noise at high gain
# keeps the inherited values), and disable the two scene-ADAPTIVE features --
# dehaze and DRC -- on principle: two cameras seeing different scene halves
# must not make independent content-reactive tone/contrast decisions, or the
# stitched pano gets a moving seam. Static tone comes from the RPi gamma.

# dehaze: fully off
dh = isp['adehaze_calib_v11']['DehazeTuningPara']
dh['Enable'] = 0
dh['dehaze_setting']['en'] = 0
if 'enhance_setting' in dh:
    dh['enhance_setting']['en'] = 0
print('dehaze: DISABLED (was enabled, scene-adaptive)')

# DRC: adaptive tone off
isp['adrc_calib_v11']['DrcTuningPara']['Enable'] = 0
print('DRC: DISABLED (was enabled, scene-adaptive)')

# per-ISO scaling: full effect at ISO<=200, half effect at 400, untouched above
def _rows(mod):
    for setting in isp[mod]['TuningPara']['Setting']:
        for row in setting['Tuning_ISO']:
            if row['iso'] <= 200:
                yield row, 1.0
            elif row['iso'] == 400:
                yield row, 0.5

YNR_SCALE = 0.5     # halve luma spatial NR strength at base ISO
for row, eff in _rows('ynr_v3'):
    for f in ('low_weight', 'high_weight', 'low_filt1_strength',
              'low_filt2_strength', 'low_bf1', 'low_bf2'):
        row[f] = r6(row[f] * (1 - eff * (1 - YNR_SCALE)))
print(f'ynr_v3: low-ISO spatial strengths x{YNR_SCALE}')

B2D_SCALE = 0.5     # raw-domain 2D NR likewise
for row, eff in _rows('bayer2dnr_v2'):
    for f in ('filter_strength', 'weight'):
        row[f] = r6(row[f] * (1 - eff * (1 - B2D_SCALE)))
print(f'bayer2dnr_v2: low-ISO filter_strength/weight x{B2D_SCALE}')

CNR_SCALE = 0.6     # chroma NR: milder cut (color blotch shows faster)
for row, eff in _rows('cnr_v2'):
    for f in ('hf_denoise_strength', 'lf_denoise_strength',
              'thumb_denoise_strength'):
        row[f] = r6(row[f] * (1 - eff * (1 - CNR_SCALE)))
print(f'cnr_v2: low-ISO denoise strengths x{CNR_SCALE}')

# sharpening: ratio 15 -> 6 at base ISO (taper via eff at 400)
SHARP_TARGET = 6.0
for row, eff in _rows('sharp_v4'):
    row['sharp_ratio'] = r6(row['sharp_ratio']
                            + eff * (SHARP_TARGET - row['sharp_ratio']))
print(f'sharp_v4: low-ISO sharp_ratio -> {SHARP_TARGET}')

# ================================================================ write + validate
# Serialize in the cJSON style rkaiq itself writes (and the skeleton ships in):
# tab indentation, '":\t"' separator, scalar arrays inline on one line.
def fmt_num(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)

def ser(o, ind):
    tabs = '\t' * ind
    if isinstance(o, dict):
        if not o:
            return '{}'
        items = [f'{tabs}\t"{k}":\t{ser(v, ind + 1)}' for k, v in o.items()]
        return '{\n' + ',\n'.join(items) + '\n' + tabs + '}'
    if isinstance(o, list):
        if not o:
            return '[]'
        if all(isinstance(v, (int, float, bool)) for v in o):
            return '[' + ', '.join(fmt_num(v) for v in o) + ']'
        if all(isinstance(v, str) for v in o):
            return '[' + ', '.join(json.dumps(v) for v in o) + ']'
        return '[' + ', '.join(ser(v, ind + 1) for v in o) + ']'
    if isinstance(o, str):
        return json.dumps(o)
    return fmt_num(o)

path = BASE + '/iqfiles/imx477_RPI-HQ_default.json'
with open(path, 'w') as f:
    f.write(ser(out, 0) + '\n')
reparsed = json.load(open(path))
assert reparsed == out, 'round-trip mismatch'
print('wrote + validated (round-trip equal)', path)
