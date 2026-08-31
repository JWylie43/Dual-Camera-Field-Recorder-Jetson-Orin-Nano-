# RPi HQ Camera → Radxa Rock 5B+ CAM0 — Flex PCB Adapter

Single-piece flexible PCB that connects a Raspberry Pi HQ Camera (IMX477,
15-pin 1.0 mm FFC) to a Radxa Rock 5B+ CAM0 port (31-pin 0.3 mm FFC).
Bare gold fingers at both ends (no connectors mounted); a stiffened
component island mid-ribbon carries a TXS0104E I²C/EN level translator
(Rock side is 1.8 V, camera side is 3.3 V) and an XC6206 1.8 V LDO.

**Status: FIRST PASS, generated.** ERC 0/0, DRC 0/0 (all severities),
netlist verified pin-for-pin against the spec table — but several physical
parameters are placeholders. Do NOT order boards before the human
checklist below is complete.

## Files

| File | What |
|---|---|
| `rock5_cam_flex.kicad_pro/.kicad_sch/.kicad_pcb` | KiCad project (authored in KiCad-8-compatible format, saved/validated with KiCad 10.0.6) |
| `flexadapter.kicad_sym`, `footprints.pretty/` | project-local libraries (see `sym-lib-table` / `fp-lib-table`) |
| `rock5_cam_flex.net` | exported netlist (kicadsexpr) |
| `bom.csv` | BOM with LCSC numbers for U1/U2 |
| `fabrication-notes.txt` | flex stackup, stiffeners, bend zones, verification list |
| `tools/generate_fingers.py` | parametric finger footprint generator (keep — pad dims are placeholders) |
| `tools/generate_schematic.py` | schematic generator (plain python3) |
| `tools/generate_pcb.py` | PCB generator (run with KiCad's bundled python, see file header) |
| `tools/adapter_netlist.py` | single source of truth for connectivity |
| `tools/check_netlist.py` | asserts the exported netlist against the spec table |

Regenerate everything:

```bash
cd tools
python3 generate_fingers.py
python3 generate_schematic.py
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 generate_pcb.py
cd ..
kicad-cli sch erc rock5_cam_flex.kicad_sch --severity-all --output erc.rpt
kicad-cli sch export netlist --format kicadsexpr --output rock5_cam_flex.net rock5_cam_flex.kicad_sch
python3 tools/check_netlist.py
kicad-cli pcb drc rock5_cam_flex.kicad_pcb --severity-all --output drc.rpt
```

## What is verified

- **Netlist** — every row of the spec table asserted by `tools/check_netlist.py`
  (MIPI pairs direct, I²C/EN through the correct TXS0104E channels with 0R
  insurance resistors, VCCA=1.8 V ≤ VCCB=3.3 V, J1 pins 30/31 (5V) and all
  other NC pins verified unconnected).
- **TXS0104E / XC6206 pinouts** — checked against the TI datasheet
  (SCES651K, D/PW package table) and Torex XC6206 SOT-23 pinout.
- **ERC and DRC** — 0 errors, 0 warnings, 0 unconnected items (with the
  relaxed flex design rules described in the fab notes).
- **MIPI intra-pair skew** — computed from actual track geometry and
  trombone-matched: D0 = 0 µm, D1 = 41 µm, CLK = 41 µm (target ±100 µm).
  Inter-pair spread ≈ 2.4 mm (spec allows ±2 mm loosely — acceptable).

## What is PLACEHOLDER / unverified (human checklist before ordering)

1. **Finger pad dimensions** (width/length/tab width at both ends) — verify
   against SHETIME FPC31-T1S1-2023-AC (LCSC C53403949), Molex 15031-0431,
   and a caliper-measured genuine RPi camera ribbon; then edit
   `tools/generate_fingers.py` and regenerate.
2. **Contact-face orientation** — both finger fields are copper-up (same
   side as components), and the 15P footprint bakes in a pin-1 mirroring
   choice. Confirm against both receptacles' contact sides and the ribbon
   bend path; mirror an end if required (`pin1` parameter in the generator).
3. **Ribbon length** — currently 80 mm (`RIBBON_LEN` in `generate_pcb.py`,
   spec range 60–100 mm). Set the final length before fab; the lane
   routing scales automatically, re-run and re-check DRC.
4. **Impedance** — not controlled; see fab notes.
5. **Fab capability** — 0.08 mm min clearance, 0.10 mm min track,
   0.35/0.15 vias. Standard for flex vendors but confirm.

## Deviations from the spec (all deliberate, review each)

- **"No vias on MIPI" is physically impossible here**: Rock CAM0 orders the
  pairs (top→bottom) D1, D0, CLK; the camera wants D0, D1, CLK. One pair
  must cross the other, and a 2-layer board can only do that with a layer
  swap. The **D0 pair does one short B.Cu hop at the J1 escape** (4 vias,
  0.35/0.15, x = 3.6–7.7 mm, outside the bend zones); D1 and CLK never
  leave F.Cu. The hatch is continuous under the hop. If the human mirrors
  an end during orientation verification, re-check whether the crossing
  disappears (some orientation combinations don't need it).
- **GND fingers 13/16/19 (J1) have no stitching vias** — at 0.3 mm pitch,
  between live MIPI rows, no 0.35 mm via fits. They run the full ribbon as
  F.Cu **shield traces between the MIPI pairs** (a signal-integrity bonus)
  and join the GND fingers at the roomy 1.0 mm end. GND 1/4/7/10/21 have
  normal escape+via stitching; one 0.10 mm-wide squeeze on the GND13 trace
  passes the D0 escape.
- **C5/C6 ("camera-end" decoupling) sit at the island exit** (~25 mm from
  J2) rather than adjacent to J2: the spec also demands "no component pads
  on unreinforced flex", and the J2 tab stiffener is the insertion tip.
  The camera module has its own local decoupling; C5/C6 serve as bulk for
  the cable run. If true J2-adjacent decoupling is wanted, add a fourth
  stiffener zone in the taper and move them.
- **TP3 added** (not in the spec BOM): the spec's JP1 connects J1.27
  (CM_RST_L_1) "to a test pad" — TP3 is that pad.
- **R1 pulls OE up to VCCA** — per the spec's own deviation note: no
  controller exists on this cable to drive OE; always-enabled is intended
  (noted on the schematic sheet).

## Design overview

```
x=0                 9      19  22            56  57     67  68  71      80
|31P fingers|--gap--|BEND A|---|== ISLAND ==|---|BEND B|---|taper|15P fingers|
 (Rock CAM0)                    U1 TXS0104E                       (RPi camera)
                                U2 XC6206 1.8V
```

- MIPI on the −y half: D0 (top lanes), D1 (inner lanes), CLK (stays on its
  J1 pad rows), each pair 0.15/0.15 coupled, GND shield traces between them.
- I²C/EN/power on the +y half through the island: J1 (1.8 V) → U1 A-side;
  U1 B-side (3.3 V) → R2/R3/R4 (0R) → J2. XVS spare channel broken out to
  TP1 (1.8 V) / TP2 (3.3 V). 3V3 trunk 0.4 mm.
- B.Cu: cross-hatched GND (0.2/0.3 mm, 45°), brief B.Cu hops only for
  CAM_EN (×2), VCCB feed, VCC_1V8 distribution and the D0 swap.
