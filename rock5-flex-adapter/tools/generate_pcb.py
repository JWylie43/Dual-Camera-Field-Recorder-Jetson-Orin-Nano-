#!/usr/bin/env python3
"""Generate rock5_cam_flex.kicad_pcb via the pcbnew API.

Run with KiCad's bundled Python:
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 generate_pcb.py

Board coordinate system (mm): x=0 at the 31P (Rock) finger edge, +x toward
the 15P (camera) end; y=0 on the ribbon centerline, +y toward the component
island bulge. MIPI runs on the -y half, I2C/EN/power on the +y half.

Layout summary:
  - Ribbon RIBBON_LEN long, 10mm wide; island bulge x 20..56 (stiffened, no
    bend); taper to the 16mm-wide 15P tab at the far end.
  - MIPI diff pairs on F.Cu. The Rock CAM0 and RPi camera pinouts put D0/D1
    in opposite vertical order, so the D1 pair does one short B.Cu hop UNDER
    THE STIFFENED ISLAND (deviation from the "no vias on MIPI" rule --
    physically unavoidable on 2 layers; see README).
  - Intra-pair length match: computed exactly, trombone bump added to the
    short member if skew > MATCH_TOL.
  - B.Cu is a cross-hatched GND zone (0.2mm line / 0.3mm gap), stitched to
    the F.Cu GND fingers with 0.4/0.2 vias at both ends and on the island.
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapter_netlist import NETS, PARTS, sanity_check

import pcbnew
from pcbnew import FromMM as MM
from pcbnew import VECTOR2I, EDA_ANGLE, DEGREES_T

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, "..")
OUT = os.path.join(PROJ, "rock5_cam_flex.kicad_pcb")
FPLIB = os.path.join(PROJ, "footprints.pretty")

# ---------------------------------------------------------------------------
# Parameters (human: tune before fab)
# ---------------------------------------------------------------------------
RIBBON_LEN = 80.0     # overall length, J1 edge to J2 edge (60-100 per spec)
RIBBON_HW = 5.0       # ribbon half-width (10mm ~= 31P tab width)
TAB2_HW = 8.0         # 15P tab half-width (16mm = RPi cable width)
TAPER_X0 = RIBBON_LEN - 12.0   # taper start (68)
TAPER_X1 = RIBBON_LEN - 9.0    # taper end (71)
ISL_X0, ISL_X1 = 20.0, 56.0    # island incl. 45-deg transitions
ISL_Y = 11.0                   # island bulge depth (+y)

W_SIG = 0.15          # signal / MIPI track width
W_I2C = 0.2           # I2C / EN / aux width
W_PWR = 0.4           # 3V3 trunk width
W_MIN = 0.1           # squeeze width (GND13 shield past the D0 escape)
VIA_D, VIA_DRL = 0.35, 0.15   # flex-fab minimum-ish via (annular 0.1)
CLR = 0.08            # min clearance (3/3 mil flex capability)
MATCH_TOL = 0.1       # intra-pair length match target (mm)

L = RIBBON_LEN
FCU, BCU = None, None  # set after import


def pt(x, y):
    return VECTOR2I(MM(x), MM(y))


# ---------------------------------------------------------------------------
board = pcbnew.CreateEmptyBoard()
FCU, BCU = pcbnew.F_Cu, pcbnew.B_Cu

bds = board.GetDesignSettings()
bds.m_MinClearance = MM(CLR)
bds.m_TrackMinWidth = MM(0.1)
bds.m_ViasMinSize = MM(VIA_D)
bds.m_MinThroughDrill = MM(VIA_DRL)
bds.m_CopperEdgeClearance = MM(0.0)
bds.m_HoleClearance = MM(0.2)
bds.m_HoleToHoleMin = MM(0.25)
bds.m_SolderMaskToCopperClearance = MM(0.0)

# nets --------------------------------------------------------------------
netmap = {}
for name in NETS:
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni)
    netmap[name] = ni

# net of every (ref, pin) -----------------------------------------------------
pin_net = {}
for name, members in NETS.items():
    for ref, p in members:
        pin_net[(ref, str(p))] = name

# footprints ------------------------------------------------------------------
# ref -> (footprint name, x, y, rot_degrees)
PLACEMENTS = {
    "J1": ("Flex_Finger_31P_0.3mm", 0.0, 0.0, 0),
    "J2": ("Flex_Finger_15P_1.0mm", L, 0.0, 180),
    "U1": ("TSSOP-14_4.4x5mm_P0.65mm", 36.0, 4.0, 0),
    "U2": ("SOT-23-3", 33.0, 9.3, 0),
    "C1": ("C_0402_1005Metric", 36.6, 9.3, 0),
    "C2": ("C_0402_1005Metric", 29.6, 10.25, 180),
    "C3": ("C_0402_1005Metric", 31.1, 1.4, 180),
    "C4": ("C_0402_1005Metric", 41.0, 1.45, 180),
    "C5": ("C_0402_1005Metric", 44.5, 9.9, 0),
    "C6": ("C_0603_1608Metric", 47.9, 9.9, 0),
    "R1": ("R_0402_1005Metric", 42.3, 5.44, 90),
    "R2": ("R_0402_1005Metric", 44.0, 2.7, 0),
    "R3": ("R_0402_1005Metric", 47.0, 3.35, 0),
    "R4": ("R_0402_1005Metric", 49.0, 4.0, 0),
    "TP1": ("TestPoint_Pad_1.5x1.5mm", 29.9, 5.4, 0),
    "TP2": ("TestPoint_Pad_1.5x1.5mm", 39.5, 9.0, 0),
    "TP3": ("TestPoint_Pad_1.5x1.5mm", 49.3, 6.6, 0),
    "JP1": ("SolderJumper-2_P1.3mm_Open_RoundedPad1.0x1.5mm", 46.0, 6.6, 0),
}

fps = {}
for ref, (name, x, y, rot) in PLACEMENTS.items():
    fp = pcbnew.FootprintLoad(FPLIB, name)
    assert fp is not None, f"footprint {name} not found in {FPLIB}"
    fp.SetReference(ref)
    fp.SetValue(PARTS[ref][0])
    fp.SetPosition(pt(x, y))
    if rot:
        fp.SetOrientation(EDA_ANGLE(rot, DEGREES_T))
    # reference text on F.Fab (silk would clutter the tiny flex)
    fp.Reference().SetLayer(pcbnew.F_Fab)
    fp.Reference().SetTextSize(pcbnew.VECTOR2I(MM(0.6), MM(0.6)))
    fp.Reference().SetTextThickness(MM(0.1))
    board.Add(fp)
    for pad in fp.Pads():
        net = pin_net.get((ref, pad.GetNumber()))
        if net:
            pad.SetNet(netmap[net])
    fps[ref] = fp


def pad_xy(ref, num):
    p = fps[ref].FindPadByNumber(str(num)).GetCenter()
    return (pcbnew.ToMM(p.x), pcbnew.ToMM(p.y))


# sanity: J1 pad rows are 0.3mm pitch, pin1 at -4.5; J2 pin1 at -7
assert abs(pad_xy("J1", 1)[1] - (-4.5)) < 1e-6, pad_xy("J1", 1)
assert abs(pad_xy("J1", 31)[1] - 4.5) < 1e-6
assert abs(pad_xy("J2", 1)[1] - (-7.0)) < 1e-6, \
    f"J2 pin1 expected at y=-7, got {pad_xy('J2', 1)} -- check finger mirroring"
assert abs(pad_xy("J2", 15)[1] - 7.0) < 1e-6

# ---------------------------------------------------------------------------
# tracks & vias
# ---------------------------------------------------------------------------
track_paths = {}   # net -> list of (layer, width, [pts]) for length reporting


def add_track(net, pts_list, width=W_SIG, layer=None):
    layer = FCU if layer is None else layer
    for a, b in zip(pts_list, pts_list[1:]):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pt(*a))
        t.SetEnd(pt(*b))
        t.SetWidth(MM(width))
        t.SetLayer(layer)
        t.SetNet(netmap[net])
        board.Add(t)
    track_paths.setdefault(net, []).append((layer, width, list(pts_list)))


def add_via(net, x, y, d=VIA_D, drill=VIA_DRL):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pt(x, y))
    v.SetDrill(MM(drill))
    v.SetWidth(MM(d))
    v.SetNet(netmap[net])
    board.Add(v)


def path_len(pts_list):
    return sum(math.dist(a, b) for a, b in zip(pts_list, pts_list[1:]))


def with_bump(pts_list, seg_idx, xm, depth, direction):
    """Insert a 45-deg trombone bump (extra length = 2*depth*(sqrt(2)-1))
    into the horizontal segment seg_idx of pts_list, centered at x=xm,
    bulging toward +y (direction=+1) or -y (-1)."""
    (x1, y1), (x2, y2) = pts_list[seg_idx], pts_list[seg_idx + 1]
    assert abs(y1 - y2) < 1e-9 and x2 > x1, "bump needs a horizontal +x segment"
    d = depth
    w = 0.4  # flat top width
    xa = xm - d - w / 2.0
    xb = xm + d + w / 2.0
    assert x1 + 0.5 < xa and xb < x2 - 0.5, "bump does not fit in segment"
    yb = y1 + direction * d
    new = pts_list[:seg_idx + 1] + [(xa, y1), (xa + d, yb), (xb - d, yb),
                                    (xb, y1)] + pts_list[seg_idx + 1:]
    return new


# ---- MIPI diff pairs --------------------------------------------------------
# Rock CAM0 orders the pairs (top->bottom) D1, D0, CLK; the camera wants
# D0, D1, CLK -- so exactly one pair swap is needed. D0 does it in ONE short
# B.Cu hop right at the J1 escape (4 vias total, the documented deviation),
# surfacing already on the top lanes. D1 and CLK never leave F.Cu.
#   T1/T2 = top lanes (D0), I1/I2 = inner lanes (D1), CLK keeps its pad rows.
T1, T2 = -4.45, -4.05
I1, I2 = -2.70, -2.40

j1 = {n: pad_xy("J1", n) for n in (11, 12, 14, 15, 17, 18)}
j2 = {n: pad_xy("J2", n) for n in (2, 3, 5, 6, 8, 9)}

# D0: dive to B.Cu at the escape, cross over D1, surface on the top lanes.
d0n_a = [j1[14], (3.45, -0.6), (3.6, -0.66)]
d0n_b = [(3.6, -0.66), (6.6, T1)]                         # B.Cu
d0n_c = [(6.6, T1), (69.25, T1), (70.8, -6.0), j2[2]]
d0p_a = [j1[15], (4.75, -0.3), (4.9, -0.45)]
d0p_b = [(4.9, -0.45), (7.7, T2)]                         # B.Cu
d0p_c = [(7.7, T2), (70.6, T2), (71.55, -5.0), j2[3]]

# D1: plain F.Cu diagonals from the pads down to the inner lanes.
d1n = [j1[11], (3.7, -1.5), (4.9, I1), (74.2, I1), (74.5, -3.0), j2[5]]
d1p = [j1[12], (4.6, -1.2), (5.8, I2), (74.0, I2), (74.4, -2.0), j2[6]]

# CLK: stays on its J1 pad rows (0.3 / 0.6), tiny fan at J2.
clkn = [j1[17], (74.3, 0.3), (74.6, 0.0), j2[8]]
clkp = [j1[18], (73.6, 0.6), (74.0, 1.0), j2[9]]

pair_report = []


def route_pair(name, n_segs, p_segs, n_bump, p_bump):
    """n_segs/p_segs: list of (pts, layer). *_bump: (seg_ref, seg_idx, xm, dir)
    identifying the horizontal segment that may receive a trombone bump."""
    ln = sum(path_len(p) for p, _ in n_segs)
    lp = sum(path_len(p) for p, _ in p_segs)
    skew = ln - lp
    if abs(skew) > MATCH_TOL:
        depth = abs(skew) / (2 * (math.sqrt(2) - 1))
        assert depth <= 0.6, f"{name}: bump depth {depth:.2f} too large"
        if skew > 0:   # P shorter -> bump P
            segs, (idx, seg_i, xm, dr) = p_segs, p_bump
        else:
            segs, (idx, seg_i, xm, dr) = n_segs, n_bump
        pts, layer = segs[idx]
        segs[idx] = (with_bump(pts, seg_i, xm, depth, dr), layer)
        ln = sum(path_len(p) for p, _ in n_segs)
        lp = sum(path_len(p) for p, _ in p_segs)
    pair_report.append((name, ln, lp, ln - lp))
    return n_segs, p_segs


# Trombone bumps go on the long straight lane run of the shorter member,
# bulging into the free space away from the partner track.
d0_n_segs = [(d0n_a, FCU), (d0n_b, BCU), (d0n_c, FCU)]
d0_p_segs = [(d0p_a, FCU), (d0p_b, BCU), (d0p_c, FCU)]
d0_n_segs, d0_p_segs = route_pair(
    "MIPI_D0",
    d0_n_segs, d0_p_segs,
    n_bump=(2, 0, 45.0, -1),   # T1 run, bump toward the board edge
    p_bump=(2, 0, 45.0, +1),   # T2 run, bump toward the inner lanes
)

d1_n_segs = [(d1n, FCU)]
d1_p_segs = [(d1p, FCU)]
d1_n_segs, d1_p_segs = route_pair(
    "MIPI_D1",
    d1_n_segs, d1_p_segs,
    n_bump=(0, 2, 45.0, -1),   # I1 run, bump toward the top lanes
    p_bump=(0, 2, 45.0, +1),   # I2 run, bump toward the CLK rows
)

clk_n_segs = [(clkn, FCU)]
clk_p_segs = [(clkp, FCU)]
clk_n_segs, clk_p_segs = route_pair(
    "MIPI_CLK",
    clk_n_segs, clk_p_segs,
    n_bump=(0, 1, 40.0, -1),
    p_bump=(0, 1, 40.0, +1),
)

for net, segs in (("MIPI_D1_N", d1_n_segs), ("MIPI_D1_P", d1_p_segs),
                  ("MIPI_D0_N", d0_n_segs), ("MIPI_D0_P", d0_p_segs),
                  ("MIPI_CLK_N", clk_n_segs), ("MIPI_CLK_P", clk_p_segs)):
    for pts, layer in segs:
        add_track(net, pts, W_SIG, layer)

# D0 layer-swap vias (the documented deviation from "no vias on MIPI")
add_via("MIPI_D0_N", 3.6, -0.66)
add_via("MIPI_D0_N", 6.6, T1)
add_via("MIPI_D0_P", 4.9, -0.45)
add_via("MIPI_D0_P", 7.7, T2)

# ---- GND stitching ----------------------------------------------------------
# J1 end: escape each GND finger to a 0.4/0.2 via placed clear of the MIPI
# escape diagonals (positions hand-checked against the fan-out).
# Outer GND fingers (1, 4, 7, 10, 21) have room for short escapes + vias.
# The interstitial GND fingers 13/16/19 sit BETWEEN the MIPI pairs where no
# 0.35mm via fits at 0.3mm pitch, so they run the full ribbon as shield
# traces between the pairs and join the GND fingers at the roomy 1.0mm end.
add_track("GND", [pad_xy("J1", 1), (3.0, -4.5)], W_SIG); add_via("GND", 3.0, -4.5)
add_track("GND", [pad_xy("J1", 4), (3.0, -3.6)], W_SIG); add_via("GND", 3.0, -3.6)
add_track("GND", [pad_xy("J1", 7), (3.0, -2.7)], W_SIG); add_via("GND", 3.0, -2.7)
add_track("GND", [pad_xy("J1", 10), (2.9, -1.8), (3.0, -1.9)], W_SIG)
add_via("GND", 3.0, -1.9)
add_track("GND", [pad_xy("J1", 21), (3.0, 1.5)], W_SIG); add_via("GND", 3.0, 1.5)

# J2 end stitching vias
for pin in (1, 4, 7, 10):
    x, y = pad_xy("J2", pin)
    add_track("GND", [(x, y), (74.5, y)], W_SIG)
    add_via("GND", 74.5, y)

# GND13 shield lane: narrow squeeze at y -0.98 while the D0 escape and D1P
# row are still alive, then back to y -0.9; ends on the J2 GND7 via.
add_track("GND", [pad_xy("J1", 13), (2.75, -0.9)], W_SIG)
add_track("GND", [(2.75, -0.9), (2.9, -0.99), (5.2, -0.99), (5.35, -0.9)], W_MIN)
add_track("GND", [(5.35, -0.9), (74.4, -0.9), (74.5, -1.0)], W_SIG)
# GND16 shield lane at y 0.0 (clear the whole way), joins GND13's lane end.
add_track("GND", [pad_xy("J1", 16), (73.5, 0.0), (74.4, -0.9)], W_SIG)
# GND19 shield lane at y 0.9, joins the J2 GND10 via.
add_track("GND", [pad_xy("J1", 19), (73.2, 0.9), (74.3, 2.0), (74.5, 2.0)], W_SIG)

# ---- I2C / EN 1.8V side (J1 -> U1 A pins) ----------------------------------
u1 = {n: pad_xy("U1", n) for n in range(1, 15)}

# SCL: J1.24 (row 2.4) -> A1 (pin 2)
add_track("SCL_1V8", [pad_xy("J1", 24), (31.5, 2.4), (31.8, 2.7), u1[2]], W_I2C)
# SDA: J1.25 (row 2.7) -> A2 (pin 3)
add_track("SDA_1V8", [pad_xy("J1", 25), (30.6, 2.7), (31.25, 3.35), u1[3]], W_I2C)
# CAM_EN: J1.23 (row 2.1) -> A3 (pin 4) via a brief B.Cu hop (crosses SCL/SDA)
add_track("CAM_EN_1V8", [pad_xy("J1", 23), (22.8, 2.1), (23.3, 1.6), (23.6, 1.6)], W_I2C)
add_via("CAM_EN_1V8", 23.6, 1.6)
add_track("CAM_EN_1V8", [(23.6, 1.6), (30.7, 4.0)], W_I2C, BCU)
add_via("CAM_EN_1V8", 30.7, 4.0)
add_track("CAM_EN_1V8", [(30.7, 4.0), u1[4]], W_I2C)

# XVS spare: A4 (pin 5) -> TP1 ; B4 (pin 10) -> TP2 (B.Cu hop past B rows)
add_track("XVS_1V8", [u1[5], (31.4, 4.65), (30.65, 5.4), pad_xy("TP1", 1)], W_I2C)
add_track("XVS_3V3", [u1[10], (39.9, 4.65)], W_I2C)
add_via("XVS_3V3", 39.9, 4.65)
add_track("XVS_3V3", [(39.9, 4.65), pad_xy("TP2", 1)], W_I2C, BCU)
add_via("XVS_3V3", *pad_xy("TP2", 1))   # via-in-pad on the test pad

# ---- U1 B side -> 0R -> J2 --------------------------------------------------
add_track("SCL_LVL", [u1[13], pad_xy("R2", 1)], W_I2C)
add_track("SDA_LVL", [u1[12], pad_xy("R3", 1)], W_I2C)
add_track("CAM_EN_LVL", [u1[11], pad_xy("R4", 1)], W_I2C)

# SCL: row 2.7 -> J2.13 (y +5)
add_track("SCL", [pad_xy("R2", 2), (70.4, 2.7), (72.7, 5.0), pad_xy("J2", 13)], W_I2C)
# SDA: row 3.35 -> J2.14 (y +6)
add_track("SDA", [pad_xy("R3", 2), (69.35, 3.35), (72.0, 6.0), pad_xy("J2", 14)], W_I2C)
# CAM_EN: must end ABOVE SCL/SDA at J2 (y +3): B.Cu hop up to row 1.6
add_track("CAM_EN", [pad_xy("R4", 2), (50.2, 4.0)], W_I2C)
add_via("CAM_EN", 50.2, 4.0)
add_track("CAM_EN", [(50.2, 4.0), (52.6, 1.6)], W_I2C, BCU)
add_via("CAM_EN", 52.6, 1.6)
add_track("CAM_EN", [(52.6, 1.6), (71.3, 1.6), (72.7, 3.0), pad_xy("J2", 11)], W_I2C)

# ---- VCC_3V3 trunk ----------------------------------------------------------
# J1.28/29 merge -> trunk y 3.75 -> island corridor y 7.7 -> exit -> J2.15
add_track("VCC_3V3", [pad_xy("J1", 28), (4.0, 3.6)], W_SIG)
add_track("VCC_3V3", [pad_xy("J1", 29), (4.0, 3.9)], W_SIG)
add_track("VCC_3V3", [(4.0, 3.6), (4.0, 3.9)], W_SIG)
trunk = [(4.0, 3.75), (20.0, 3.75), (23.95, 7.7), (50.5, 7.7), (53.8, 4.4),
         (68.6, 4.4), (71.2, 7.0), pad_xy("J2", 15)]
add_track("VCC_3V3", trunk, W_PWR)

# island power taps
c1_1, c1_2 = pad_xy("C1", 1), pad_xy("C1", 2)
add_track("VCC_3V3", [pad_xy("U2", 3), c1_1], W_I2C)          # VIN row
add_track("VCC_3V3", [(35.4, 9.3), (37.0, 7.7)], W_I2C)       # VIN row -> trunk
add_track("GND", [c1_2, (38.0, 9.3)], W_I2C); add_via("GND", 38.0, 9.3)

# C4 (VCCB decoupling) + VCCB feed via B.Cu hop from the trunk
c4_1, c4_2 = pad_xy("C4", 1), pad_xy("C4", 2)
add_track("VCC_3V3", [u1[14], (40.73, 2.05), c4_1], W_I2C)    # pin14 -> C4.1
add_via("VCC_3V3", 43.0, 7.7)                                  # on trunk
add_track("VCC_3V3", [(43.0, 7.7), (42.4, 1.45)], W_I2C, BCU)
add_via("VCC_3V3", 42.4, 1.45)
add_track("VCC_3V3", [(42.4, 1.45), c4_1], W_I2C)
add_track("GND", [c4_2, (39.6, 1.45)], W_I2C); add_via("GND", 39.6, 1.45)

# C5 / C6 (camera-side bulk, placed at island exit -- see README)
add_track("VCC_3V3", [pad_xy("C5", 1), (44.02, 7.7)], W_I2C)
add_track("GND", [pad_xy("C5", 2), (46.0, 9.9)], W_I2C); add_via("GND", 46.0, 9.9)
add_track("VCC_3V3", [pad_xy("C6", 1), (47.125, 7.7)], W_I2C)
add_track("GND", [pad_xy("C6", 2), (49.7, 9.9)], W_I2C); add_via("GND", 49.7, 9.9)

# ---- VCC_1V8 (U2 LDO) --------------------------------------------------------
u2_vss, u2_vout = pad_xy("U2", 1), pad_xy("U2", 2)
c2_1, c2_2 = pad_xy("C2", 1), pad_xy("C2", 2)
add_track("VCC_1V8", [u2_vout, c2_1], W_I2C)                   # VOUT row -> C2.1
add_via("VCC_1V8", 31.0, 10.25)                                # B.Cu tree root
add_track("GND", [c2_2, (28.2, 10.25)], W_I2C); add_via("GND", 28.2, 10.25)
add_track("GND", [u2_vss, (30.3, 8.35)], W_I2C); add_via("GND", 30.3, 8.35)

# branch to C3 / U1.VCCA (upper-left)
add_track("VCC_1V8", [(31.0, 10.25), (31.6, 1.4)], W_I2C, BCU)
add_via("VCC_1V8", 31.6, 1.4)   # lands inside C3 pad 1
c3_1, c3_2 = pad_xy("C3", 1), pad_xy("C3", 2)
add_track("VCC_1V8", [(31.6, 1.4), c3_1], W_I2C)
add_track("VCC_1V8", [c3_1, (32.55, 1.4), (33.14, 1.99), u1[1]], W_I2C)
add_track("GND", [c3_2, (30.2, 1.4), (29.9, 1.5)], W_I2C); add_via("GND", 29.9, 1.5)

# branch to R1 (OE pull-up)
add_track("VCC_1V8", [(31.0, 10.25), (41.0, 9.6), (42.3, 4.6)], W_I2C, BCU)
add_via("VCC_1V8", 42.3, 4.6)
add_track("VCC_1V8", [(42.3, 4.6), pad_xy("R1", 2)], W_I2C)
add_track("U1_OE", [u1[8], pad_xy("R1", 1)], W_I2C)

# ---- U1 GND ------------------------------------------------------------------
add_track("GND", [u1[7], (32.49, 6.6), (32.2, 6.6)], W_I2C)
add_via("GND", 32.2, 6.6)

# ---- reset breakout ----------------------------------------------------------
add_track("CM_RST_L", [pad_xy("J1", 27), (21.0, 3.3), (24.8, 7.1), (43.7, 7.1),
                       (44.2, 6.6), pad_xy("JP1", 1)], W_I2C)
add_track("RST_TP", [pad_xy("JP1", 2), pad_xy("TP3", 1)], W_I2C)

# ---------------------------------------------------------------------------
# board outline
# ---------------------------------------------------------------------------
outline = [(0, -RIBBON_HW), (TAPER_X0, -RIBBON_HW), (TAPER_X1, -TAB2_HW),
           (L, -TAB2_HW), (L, TAB2_HW), (TAPER_X1, TAB2_HW),
           (TAPER_X0, RIBBON_HW), (ISL_X1, RIBBON_HW), (50.0, ISL_Y),
           (26.0, ISL_Y), (ISL_X0, RIBBON_HW), (0, RIBBON_HW)]
for a, b in zip(outline, outline[1:] + outline[:1]):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pt(*a))
    s.SetEnd(pt(*b))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(MM(0.1))
    board.Add(s)

# ---------------------------------------------------------------------------
# B.Cu hatched GND zone
# ---------------------------------------------------------------------------
z = pcbnew.ZONE(board)
z.SetLayer(BCU)
z.SetNet(netmap["GND"])
z.SetZoneName("GND_HATCH")
ol = z.Outline()
ol.NewOutline()
for x, y in outline:
    ol.Append(MM(x), MM(y))
z.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
z.SetHatchThickness(MM(0.2))
z.SetHatchGap(MM(0.3))
z.SetHatchOrientation(EDA_ANGLE(45, DEGREES_T))
z.SetHatchSmoothingLevel(0)
z.SetMinThickness(MM(0.15))
z.SetLocalClearance(MM(0.2))
z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
board.Add(z)

filler = pcbnew.ZONE_FILLER(board)
filler.Fill(board.Zones())

# ---------------------------------------------------------------------------
# stiffener zones (User.2) and bend zones (User.3) -- documentation graphics
# ---------------------------------------------------------------------------

def doc_rect(x1, y1, x2, y2, layer, label, lx, ly):
    r = pcbnew.PCB_SHAPE(board)
    r.SetShape(pcbnew.SHAPE_T_RECT)
    r.SetStart(pt(x1, y1))
    r.SetEnd(pt(x2, y2))
    r.SetLayer(layer)
    r.SetWidth(MM(0.1))
    board.Add(r)
    t = pcbnew.PCB_TEXT(board)
    t.SetText(label)
    t.SetPosition(pt(lx, ly))
    t.SetLayer(layer)
    t.SetTextSize(pcbnew.VECTOR2I(MM(0.8), MM(0.8)))
    t.SetTextThickness(MM(0.12))
    board.Add(t)


U2L, U3L = pcbnew.User_2, pcbnew.User_3
doc_rect(0, -RIBBON_HW, 3.5, RIBBON_HW, U2L,
         "STIFFENER 1: under 31P tab, finished tip 0.30+/-0.03mm", 14, -6.5)
doc_rect(74.0, -TAB2_HW, L, TAB2_HW, U2L,
         "STIFFENER 2: under 15P tab, finished tip 0.30+/-0.03mm", 62, -9.5)
doc_rect(22.0, -RIBBON_HW, ISL_X1, ISL_Y, U2L,
         "STIFFENER 3: under entire component island (incl. MIPI layer-swap vias)", 38, 12.5)
doc_rect(8.0, -RIBBON_HW, 19.0, RIBBON_HW, U3L,
         "BEND ZONE A: no components/vias", 13.5, 6.5)
doc_rect(57.0, -RIBBON_HW, 67.0, RIBBON_HW, U3L,
         "BEND ZONE B: no components/vias", 62, 6.5)

# silkscreen labels
for txt, x, y, size in (("ROCK 5B+ CAM0", 12.0, -3.35, 0.8),
                        ("RPi HQ CAM", 62.0, -3.35, 0.8),
                        ("5V NC", 8.5, 4.45, 0.8)):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(txt)
    t.SetPosition(pt(x, y))
    t.SetLayer(pcbnew.F_SilkS)
    t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    t.SetTextThickness(MM(max(0.1, size * 0.15)))
    board.Add(t)

# ---------------------------------------------------------------------------
board.Save(OUT)
print(f"wrote {OUT}")

# pcbnew's Save() rewrites the .kicad_pro with stock defaults; patch the
# Default netclass back to flex-appropriate values (0.1mm clearance).
import json
pro_path = os.path.join(PROJ, "rock5_cam_flex.kicad_pro")
with open(pro_path) as f:
    pro = json.load(f)
for nc in pro.get("net_settings", {}).get("classes", []):
    if nc.get("name") == "Default":
        nc["clearance"] = CLR
        nc["track_width"] = 0.15
        nc["diff_pair_width"] = 0.15
        nc["diff_pair_gap"] = 0.15
        nc["via_diameter"] = VIA_D
        nc["via_drill"] = VIA_DRL
with open(pro_path, "w") as f:
    json.dump(pro, f, indent=2)
print(f"patched {pro_path} (Default netclass: clearance 0.1, track 0.15)")
print("\nMIPI intra-pair length report (after matching):")
for name, ln, lp, skew in pair_report:
    print(f"  {name}: N={ln:.3f}mm  P={lp:.3f}mm  skew={skew*1000:+.0f}um")
sanity_check()
