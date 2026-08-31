#!/usr/bin/env python3
"""Parametric generator for bare FFC gold-finger footprints (flex PCB).

Regenerates the two finger footprints used by the RPi HQ Camera -> Rock 5B+
flex adapter. ALL DIMENSIONS BELOW ARE PLACEHOLDERS pending verification
against the actual receptacle datasheets:

  - 31P 0.3mm end: SHETIME FPC31-T1S1-2023-AC (LCSC C53403949) or the
    actual connector fitted on the Rock 5B+ CAM0 port.
  - 15P 1.0mm end: the RPi HQ camera's 15P 1.0mm bottom-contact receptacle
    drawing + caliper measurement of a genuine RPi camera ribbon.

Usage:  python3 generate_fingers.py   (writes into ../footprints.pretty/)

Conventions:
  - Footprint origin = center of the board edge the fingers exit through.
  - Fingers extend in +x from the edge (x=0) inward.
  - Pin 1 is at -y; a silkscreen dot marks it.
  - Pads are F.Cu + F.Mask only (bare copper, ENIG, no paste).
"""
import os
import uuid

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "footprints.pretty")

# ----------------------------------------------------------------------------
# PARAMETERS (PLACEHOLDERS -- verify against receptacle datasheets before fab)
# ----------------------------------------------------------------------------
FINGERS = [
    {
        "name": "Flex_Finger_31P_0.3mm",
        "descr": "Bare gold fingers, 31 contacts, 0.3mm pitch, for Rock 5B+ CAM0 "
                 "31P FFC receptacle. PLACEHOLDER dims pending datasheet check.",
        "pins": 31,
        "pitch": 0.3,
        "pad_w": 0.18,   # conductor width, typ. 0.3mm-pitch FFC (0.18 +/- 0.03)
        "pad_l": 2.5,    # exposed contact length, typ. 0.3mm-pitch FFC
        "tab_w": 10.0,   # overall tab width: (31-1)*0.3 + 1.0 margin
        "tab_l": 3.5,    # stiffened tab length (courtyard / stiffener hint)
    },
    {
        "name": "Flex_Finger_15P_1.0mm",
        "descr": "Bare gold fingers, 15 contacts, 1.0mm pitch, for RPi HQ camera "
                 "15P FFC receptacle. PLACEHOLDER dims pending datasheet check.",
        "pins": 15,
        "pitch": 1.0,
        "pad_w": 0.60,   # conductor width, typ. 1.0mm-pitch FFC
        "pad_l": 4.0,    # exposed contact length, typ. RPi camera ribbon
        "tab_w": 16.0,   # overall tab width: matches genuine RPi camera cable
        "tab_l": 5.0,
        # Pin 1 at +y so that the 180-deg placement at the far board edge
        # puts pin 1 back at the top. This bakes in ONE assumption about
        # contact face vs. ribbon bend path -- UNVERIFIED, human must check
        # against the receptacle drawing and mirror if wrong (see README).
        "pin1": "+y",
    },
]


def gen_footprint(p):
    lines = []
    a = lines.append
    a(f'(footprint "{p["name"]}"')
    a('  (version 20240108)')
    a('  (generator "generate_fingers.py")')
    a('  (layer "F.Cu")')
    a(f'  (descr "{p["descr"]}")')
    a('  (tags "flex ffc gold finger placeholder")')
    a('  (attr smd exclude_from_pos_files exclude_from_bom)')

    n = p["pins"]
    pitch = p["pitch"]
    pad_w = p["pad_w"]
    pad_l = p["pad_l"]
    tab_w = p["tab_w"]
    tab_l = p["tab_l"]
    span = (n - 1) * pitch

    # Reference / value text on fab layer only (silk would sit on the tab)
    a(f'  (property "Reference" "REF**" (at {tab_l + 2:.3f} 0 0) (layer "F.Fab")'
      f' (uuid "{uuid.uuid4()}") (effects (font (size 0.7 0.7) (thickness 0.1))))')
    a(f'  (property "Value" "{p["name"]}" (at {tab_l + 4:.3f} 0 0) (layer "F.Fab")'
      f' (uuid "{uuid.uuid4()}") (effects (font (size 0.7 0.7) (thickness 0.1))))')
    a(f'  (property "Datasheet" "" (at 0 0 0) (layer "F.Fab") hide'
      f' (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))')
    a(f'  (property "Description" "" (at 0 0 0) (layer "F.Fab") hide'
      f' (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))')

    # Pads: pin 1 at -y (or +y if p["pin1"] == "+y"); contacts run from the
    # board edge inward, set back 0.1mm from the tip (DRC edge clearance).
    setback = 0.1
    sign = -1.0 if p.get("pin1") == "+y" else 1.0
    for i in range(1, n + 1):
        y = sign * ((i - 1) * pitch - span / 2.0)
        a(f'  (pad "{i}" smd rect (at {setback + pad_l / 2.0:.4f} {y:.4f}) '
          f'(size {pad_l:.4f} {pad_w:.4f}) (layers "F.Cu" "F.Mask") '
          f'(uuid "{uuid.uuid4()}"))')

    # Pin-1 marker (silkscreen dot next to pin 1, clear of the pads)
    y1 = sign * (-span / 2.0)
    a(f'  (fp_circle (center {pad_l + 0.6:.3f} {y1:.3f}) (end {pad_l + 0.75:.3f} {y1:.3f})'
      f' (stroke (width 0.15) (type solid)) (fill solid) (layer "F.SilkS")'
      f' (uuid "{uuid.uuid4()}"))')

    # Fab outline: the tab
    hw = tab_w / 2.0
    a(f'  (fp_rect (start 0 {-hw:.3f}) (end {tab_l:.3f} {hw:.3f})'
      f' (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab")'
      f' (uuid "{uuid.uuid4()}"))')

    # Courtyard
    a(f'  (fp_rect (start 0 {-hw - 0.25:.3f}) (end {tab_l + 0.25:.3f} {hw + 0.25:.3f})'
      f' (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd")'
      f' (uuid "{uuid.uuid4()}"))')

    a(')')
    return "\n".join(lines) + "\n"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in FINGERS:
        path = os.path.join(OUT_DIR, p["name"] + ".kicad_mod")
        with open(path, "w") as f:
            f.write(gen_footprint(p))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
