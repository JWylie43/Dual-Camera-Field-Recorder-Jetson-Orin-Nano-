#!/usr/bin/env python3
"""Generate rock5_cam_flex.kicad_sch (KiCad 8 s-expression format).

Single sheet, left-to-right: J1 (Rock 5B+ CAM0, 31P) | level-translator +
power block | J2 (RPi camera, 15P). Connectivity is expressed with net
labels; every pin gets either a short wire stub + label or a no-connect
flag, so ERC is clean.

Run:  python3 generate_schematic.py   (writes ../rock5_cam_flex.kicad_sch)
"""
import os
import uuid

from adapter_netlist import (NETS, NC_PINS, J1_PIN_NAMES, J2_PIN_NAMES,
                             PARTS, net_of, sanity_check)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "rock5_cam_flex.kicad_sch")
PROJECT = "rock5_cam_flex"
ROOT_UUID = "a0b1c2d3-0000-4000-8000-000000000001"

FONT = '(effects (font (size 1.27 1.27)))'


def u():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Embedded symbol library
# ---------------------------------------------------------------------------

def sym_header(name, ref_prefix, value, extra=""):
    return (f'    (symbol "flexadapter:{name}" {extra}(pin_names (offset 1.016)) '
            f'(exclude_from_sim no) (in_bom yes) (on_board yes)\n'
            f'      (property "Reference" "{ref_prefix}" (at 0 2.54 0) {FONT})\n'
            f'      (property "Value" "{value}" (at 0 -2.54 0) {FONT})\n'
            f'      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
            f'      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
            f'      (property "Description" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n')


def pin(etype, x, y, angle, length, name, number):
    return (f'        (pin {etype} line (at {x} {y} {angle}) (length {length})\n'
            f'          (name "{name}" {FONT}) (number "{number}" {FONT}))\n')


def rect(x1, y1, x2, y2):
    return (f'        (rectangle (start {x1} {y1}) (end {x2} {y2}) '
            f'(stroke (width 0.254) (type default)) (fill (type background)))\n')


def conn_symbol(name, npins, pin_names, side):
    """Single-column connector. side='right': pins exit right; 'left': exit left."""
    s = sym_header(name, "J", name)
    half = (npins - 1) / 2.0
    top = half * 2.54 + 2.54
    if side == "right":
        s += f'      (symbol "{name}_0_1"\n' + rect(-15.24, top, 0, -top) + '      )\n'
    else:
        s += f'      (symbol "{name}_0_1"\n' + rect(0, top, 15.24, -top) + '      )\n'
    s += f'      (symbol "{name}_1_1"\n'
    for i in range(1, npins + 1):
        y = (half - (i - 1)) * 2.54  # pin 1 on top
        if side == "right":
            s += pin("passive", 2.54, round(y, 2), 180, 2.54, pin_names[i], i)
        else:
            s += pin("passive", -2.54, round(y, 2), 0, 2.54, pin_names[i], i)
    s += '      )\n    )\n'
    return s


def txs0104e_symbol():
    s = sym_header("TXS0104E", "U", "TXS0104EPWR")
    s += '      (symbol "TXS0104E_0_1"\n' + rect(-7.62, 8.89, 7.62, -10.16) + '      )\n'
    s += '      (symbol "TXS0104E_1_1"\n'
    s += pin("power_in", -10.16, 6.35, 0, 2.54, "VCCA", 1)
    s += pin("bidirectional", -10.16, 2.54, 0, 2.54, "A1", 2)
    s += pin("bidirectional", -10.16, 0, 0, 2.54, "A2", 3)
    s += pin("bidirectional", -10.16, -2.54, 0, 2.54, "A3", 4)
    s += pin("bidirectional", -10.16, -5.08, 0, 2.54, "A4", 5)
    s += pin("input", -10.16, -8.89, 0, 2.54, "OE", 8)
    s += pin("power_in", 10.16, 6.35, 180, 2.54, "VCCB", 14)
    s += pin("bidirectional", 10.16, 2.54, 180, 2.54, "B1", 13)
    s += pin("bidirectional", 10.16, 0, 180, 2.54, "B2", 12)
    s += pin("bidirectional", 10.16, -2.54, 180, 2.54, "B3", 11)
    s += pin("bidirectional", 10.16, -5.08, 180, 2.54, "B4", 10)
    s += pin("power_in", 0, -12.7, 90, 2.54, "GND", 7)
    s += pin("no_connect", -2.54, -12.7, 90, 2.54, "NC", 6)
    s += pin("no_connect", 2.54, -12.7, 90, 2.54, "NC", 9)
    s += '      )\n    )\n'
    return s


def xc6206_symbol():
    s = sym_header("XC6206P182", "U", "XC6206P182MR")
    s += '      (symbol "XC6206P182_0_1"\n' + rect(-5.08, 3.81, 5.08, -3.81) + '      )\n'
    s += '      (symbol "XC6206P182_1_1"\n'
    s += pin("power_in", -7.62, 1.27, 0, 2.54, "VIN", 3)
    s += pin("power_out", 7.62, 1.27, 180, 2.54, "VOUT", 2)
    s += pin("power_in", 0, -6.35, 90, 2.54, "VSS", 1)
    s += '      )\n    )\n'
    return s


def rc_symbol(name, ref_prefix):
    s = sym_header(name, ref_prefix, name)
    s += f'      (symbol "{name}_0_1"\n'
    if ref_prefix == "R":
        s += rect(-1.016, 2.54, 1.016, -2.54)
    else:
        s += ('        (polyline (pts (xy -1.905 0.508) (xy 1.905 0.508)) '
              '(stroke (width 0.3) (type default)) (fill (type none)))\n'
              '        (polyline (pts (xy -1.905 -0.508) (xy 1.905 -0.508)) '
              '(stroke (width 0.3) (type default)) (fill (type none)))\n')
    s += '      )\n'
    s += f'      (symbol "{name}_1_1"\n'
    s += pin("passive", 0, 3.81, 270, 1.27 if ref_prefix == "C" else 1.27, "~", 1)
    s += pin("passive", 0, -3.81, 90, 1.27 if ref_prefix == "C" else 1.27, "~", 2)
    s += '      )\n    )\n'
    return s


def tp_symbol():
    s = sym_header("TestPoint", "TP", "TestPoint")
    s += ('      (symbol "TestPoint_0_1"\n'
          '        (circle (center 0 2.032) (radius 0.762) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n      )\n')
    s += '      (symbol "TestPoint_1_1"\n'
    s += pin("passive", 0, 0, 90, 1.27, "TP", 1)
    s += '      )\n    )\n'
    return s


def jumper_symbol():
    s = sym_header("SolderJumper_Open", "JP", "SolderJumper_Open")
    s += ('      (symbol "SolderJumper_Open_0_1"\n'
          '        (arc (start -0.508 1.016) (mid -1.524 0) (end -0.508 -1.016) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n'
          '        (polyline (pts (xy -0.508 1.016) (xy -0.508 -1.016)) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n'
          '        (arc (start 0.508 -1.016) (mid 1.524 0) (end 0.508 1.016) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n'
          '        (polyline (pts (xy 0.508 1.016) (xy 0.508 -1.016)) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n      )\n')
    s += '      (symbol "SolderJumper_Open_1_1"\n'
    s += pin("passive", -3.81, 0, 0, 2.286, "A", 1)
    s += pin("passive", 3.81, 0, 180, 2.286, "B", 2)
    s += '      )\n    )\n'
    return s


def pwr_flag_symbol():
    s = sym_header("PWR_FLAG", "#FLG", "PWR_FLAG", extra="(power) ")
    s += ('      (symbol "PWR_FLAG_0_1"\n'
          '        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) '
          '(xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) '
          '(stroke (width 0.254) (type default)) (fill (type none)))\n      )\n')
    s += '      (symbol "PWR_FLAG_1_1"\n'
    s += ('        (pin power_out line (at 0 0 90) (length 0) hide\n'
          f'          (name "pwr" {FONT}) (number "1" {FONT}))\n')
    s += '      )\n    )\n'
    return s


# ---------------------------------------------------------------------------
# Canvas helpers (canvas y grows DOWN; symbol y grows UP)
# ---------------------------------------------------------------------------

body = []          # accumulated s-expressions for wires/labels/etc.
symbols = []       # accumulated symbol instances


def wire(x1, y1, x2, y2):
    body.append(f'  (wire (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f})) '
                f'(stroke (width 0) (type default)) (uuid "{u()}"))')


def label(name, x, y, justify):
    body.append(f'  (label "{name}" (at {x:.2f} {y:.2f} 0) '
                f'(effects (font (size 1.27 1.27)) (justify {justify} bottom)) (uuid "{u()}"))')


def no_connect(x, y):
    body.append(f'  (no_connect (at {x:.2f} {y:.2f}) (uuid "{u()}"))')


def text(s, x, y, size=1.5):
    esc = s.replace('"', '\\"').replace("\n", "\\n")
    body.append(f'  (text "{esc}" (exclude_from_sim no) (at {x:.2f} {y:.2f} 0) '
                f'(effects (font (size {size} {size})) (justify left bottom)) (uuid "{u()}"))')


def place_symbol(lib, ref, x, y, pins, value=None, rot=0, extra_props=None,
                 hide_value=False):
    """Place a symbol instance; pins = list of pin numbers to declare."""
    val, footprint, lcsc = PARTS.get(ref, (value or lib, "", ""))
    if value is not None:
        val = value
    hide = " hide" if ref.startswith("#") else ""
    vhide = " hide" if hide_value else ""
    p = [f'  (symbol (lib_id "flexadapter:{lib}") (at {x:.2f} {y:.2f} {rot}) (unit 1)']
    p.append('    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)')
    p.append(f'    (uuid "{u()}")')
    p.append(f'    (property "Reference" "{ref}" (at {x:.2f} {y - 5.5:.2f} 0) '
             f'(effects (font (size 1.27 1.27)){hide}))')
    p.append(f'    (property "Value" "{val}" (at {x:.2f} {y + 5.5:.2f} 0) '
             f'(effects (font (size 1.27 1.27)){vhide}))')
    p.append(f'    (property "Footprint" "{footprint}" (at {x:.2f} {y:.2f} 0) '
             f'(effects (font (size 1.27 1.27)) hide))')
    p.append(f'    (property "Datasheet" "" (at {x:.2f} {y:.2f} 0) '
             f'(effects (font (size 1.27 1.27)) hide))')
    if lcsc:
        p.append(f'    (property "LCSC" "{lcsc}" (at {x:.2f} {y:.2f} 0) '
                 f'(effects (font (size 1.27 1.27)) hide))')
    for e in (extra_props or []):
        p.append(e)
    for pn in pins:
        p.append(f'    (pin "{pn}" (uuid "{u()}"))')
    p.append(f'    (instances (project "{PROJECT}" '
             f'(path "/{ROOT_UUID}" (reference "{ref}") (unit 1))))')
    p.append('  )')
    symbols.append("\n".join(p))


def stub(net, x, y, direction, length=10.16):
    """Wire stub from a pin at (x,y) plus a net label at the free end."""
    if direction == "right":
        wire(x, y, x + length, y)
        label(net, x + length, y, "left")
    elif direction == "left":
        wire(x, y, x - length, y)
        label(net, x - length, y, "right")
    elif direction == "down":
        wire(x, y, x, y + length)
        label(net, x, y + length, "left")
    elif direction == "up":
        wire(x, y, x, y - length)
        label(net, x, y - length, "left")


# ---------------------------------------------------------------------------
# Build the sheet
# ---------------------------------------------------------------------------

def main():
    sanity_check()

    # ---- J1 (Rock 5B+ CAM0) on the left, pins exit right -------------------
    j1x, j1y = 50.8, 139.7
    place_symbol("Conn_Flex_31P", "J1", j1x, j1y, range(1, 32))
    for i in range(1, 32):
        px, py = j1x + 2.54, j1y + (i - 16) * 2.54
        if i in NC_PINS["J1"]:
            no_connect(px, py)
        else:
            stub(net_of("J1", i), px, py, "right")

    # ---- J2 (RPi camera) on the right, pins exit left ----------------------
    j2x, j2y = 322.58, 139.7
    place_symbol("Conn_Flex_15P", "J2", j2x, j2y, range(1, 16))
    for i in range(1, 16):
        px, py = j2x - 2.54, j2y + (i - 8) * 2.54
        if i in NC_PINS["J2"]:
            no_connect(px, py)
        else:
            stub(net_of("J2", i), px, py, "left")

    # ---- U1 level translator in the middle ---------------------------------
    ux, uy = 177.8, 127.0
    place_symbol("TXS0104E", "U1", ux, uy, range(1, 15))
    left = {1: -6.35, 2: -2.54, 3: 0, 4: 2.54, 5: 5.08, 8: 8.89}
    for pn, dy in left.items():
        stub(net_of("U1", pn), ux - 10.16, uy + dy, "left")
    right = {14: -6.35, 13: -2.54, 12: 0, 11: 2.54, 10: 5.08}
    for pn, dy in right.items():
        stub(net_of("U1", pn), ux + 10.16, uy + dy, "right")
    stub("GND", ux, uy + 12.7, "down", 5.08)          # pin 7
    no_connect(ux - 2.54, uy + 12.7)                   # pin 6
    no_connect(ux + 2.54, uy + 12.7)                   # pin 9

    # ---- series resistors between U1 B-side and J2 --------------------------
    for ref, x, top_net, bot_net in (
            ("R2", 218.44, "SCL_LVL", "SCL"),
            ("R3", 231.14, "SDA_LVL", "SDA"),
            ("R4", 243.84, "CAM_EN_LVL", "CAM_EN")):
        place_symbol("R", ref, x, 127.0, (1, 2))
        stub(top_net, x, 127.0 - 3.81, "up", 2.54)
        stub(bot_net, x, 127.0 + 3.81, "down", 2.54)

    # ---- power block: LDO + decoupling row ----------------------------------
    py_row = 175.26
    place_symbol("XC6206P182", "U2", 127.0, py_row, (1, 2, 3))
    stub("VCC_3V3", 127.0 - 7.62, py_row - 1.27, "left", 7.62)
    stub("VCC_1V8", 127.0 + 7.62, py_row - 1.27, "right", 7.62)
    stub("GND", 127.0, py_row + 6.35, "down", 3.81)

    caps = (("C1", 96.52, "VCC_3V3"), ("C2", 149.86, "VCC_1V8"),
            ("C3", 162.56, "VCC_1V8"), ("C4", 199.39, "VCC_3V3"),
            ("C5", 212.09, "VCC_3V3"), ("C6", 224.79, "VCC_3V3"))
    for ref, x, topnet in caps:
        place_symbol("C", ref, x, py_row, (1, 2))
        stub(topnet, x, py_row - 3.81, "up", 2.54)
        stub("GND", x, py_row + 3.81, "down", 2.54)

    # R1: OE pull-up to VCCA (deliberate deviation from datasheet pulldown)
    place_symbol("R", "R1", 186.69, py_row, (1, 2))
    stub("U1_OE", 186.69, py_row - 3.81, "up", 2.54)
    stub("VCC_1V8", 186.69, py_row + 3.81, "down", 2.54)

    # ---- PWR_FLAG anchors ----------------------------------------------------
    place_symbol("PWR_FLAG", "#FLG01", 76.2, py_row - 5.08, (1,), hide_value=True)
    wire(76.2, py_row - 5.08, 76.2, py_row + 2.54)
    label("VCC_3V3", 76.2, py_row + 2.54, "left")
    place_symbol("PWR_FLAG", "#FLG02", 66.04, py_row - 5.08, (1,), hide_value=True)
    wire(66.04, py_row - 5.08, 66.04, py_row + 2.54)
    label("GND", 66.04, py_row + 2.54, "left")

    # ---- test points + reset jumper -----------------------------------------
    tp_row = 210.82
    for ref, x, net in (("TP1", 152.4, "XVS_1V8"), ("TP2", 165.1, "XVS_3V3"),
                        ("TP3", 177.8, "RST_TP")):
        place_symbol("TestPoint", ref, x, tp_row, (1,))
        stub(net, x, tp_row, "down", 3.81)
    place_symbol("SolderJumper_Open", "JP1", 205.74, tp_row, (1, 2))
    stub("CM_RST_L", 205.74 - 3.81, tp_row, "left", 6.35)
    stub("RST_TP", 205.74 + 3.81, tp_row, "right", 6.35)

    # ---- design notes --------------------------------------------------------
    text("RPi HQ Camera (15P 1.0mm)  ->  Radxa Rock 5B+ CAM0 (31P 0.3mm)  flex adapter\\n"
         "J1 = 31P gold fingers (Rock CAM0)      J2 = 15P gold fingers (camera)",
         40.64, 40.64, 2.0)
    text("NOTE (a): U1 OE is pulled UP to VCCA via R1 (10k). This deviates from the TXS0104E\\n"
         "datasheet's recommended pulldown: there is no controller on this cable to drive OE,\\n"
         "and always-enabled operation is intended.", 40.64, 55.88)
    text("NOTE (b): J1 pins 30/31 (VCC_5V) are INTENTIONALLY UNCONNECTED.\\n"
         "5V must never reach the camera (3.3V max).", 40.64, 68.58)
    text("NOTE (c): Gold-finger pad dimensions are PLACEHOLDERS pending receptacle\\n"
         "datasheet verification (see tools/generate_fingers.py).", 40.64, 78.74)
    text("NOTE (d): TXS0104E constraint: VCCA (1.8V, Rock side) MUST be <= VCCB (3.3V,\\n"
         "camera side). U2 (XC6206-1.8) derives VCCA from VCC_3V3.", 40.64, 88.9)
    text("Translator channels: 1=SCL  2=SDA  3=CAM_EN  4=XVS spare (TP1/TP2).\\n"
         "R2-R4 are 0R insurance footprints in SCL/SDA/CAM_EN.\\n"
         "JP1 (open) breaks out J1.27 CM_RST_L_1 to TP3 for future reset experiments.",
         40.64, 233.68)

    # ---- assemble file -------------------------------------------------------
    lib = "".join([
        conn_symbol("Conn_Flex_31P", 31, J1_PIN_NAMES, "right"),
        conn_symbol("Conn_Flex_15P", 15, J2_PIN_NAMES, "left"),
        txs0104e_symbol(),
        xc6206_symbol(),
        rc_symbol("R", "R"),
        rc_symbol("C", "C"),
        tp_symbol(),
        jumper_symbol(),
        pwr_flag_symbol(),
    ])

    out = []
    out.append('(kicad_sch (version 20231120) (generator "generate_schematic.py")')
    out.append(f'  (uuid "{ROOT_UUID}")')
    out.append('  (paper "A3")')
    out.append('  (title_block (title "RPi HQ Camera -> Rock 5B+ CAM0 flex adapter") '
               '(date "2026-08-31") (rev "0.1") '
               '(comment 1 "First pass - generated; pad dims/orientation UNVERIFIED"))')
    out.append('  (lib_symbols')
    out.append(lib.rstrip())
    out.append('  )')
    out.extend(symbols)
    out.extend(body)
    out.append('  (sheet_instances (path "/" (page "1")))')
    out.append(')')

    with open(OUT, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {OUT}")

    # Also emit the same symbols as a project-local library so the lib tables
    # resolve (symbol names unprefixed inside a .kicad_sym).
    lib_path = os.path.join(os.path.dirname(OUT), "flexadapter.kicad_sym")
    lib_content = lib.replace('(symbol "flexadapter:', '(symbol "')
    with open(lib_path, "w") as f:
        f.write('(kicad_symbol_lib (version 20231120) (generator "generate_schematic.py")\n'
                + lib_content.rstrip() + "\n)\n")
    print(f"wrote {lib_path}")


if __name__ == "__main__":
    main()
