#!/usr/bin/env python3
"""Single source of truth for the adapter's connectivity.

Derived from the engineering spec table (Radxa ROCK 5B+ V1.2 schematic p.18
"Camera_MIPI-CSI" + official RPi camera docs). Both generate_schematic.py and
generate_pcb.py consume this; check_netlist.py asserts the exported netlist
against the spec table independently.
"""

# net name -> list of (ref, pin)
NETS = {
    "GND": [("J1", 1), ("J1", 4), ("J1", 7), ("J1", 10), ("J1", 13), ("J1", 16),
            ("J1", 19), ("J1", 21),
            ("J2", 1), ("J2", 4), ("J2", 7), ("J2", 10),
            ("U1", 7), ("U2", 1),
            ("C1", 2), ("C2", 2), ("C3", 2), ("C4", 2), ("C5", 2), ("C6", 2)],
    "VCC_3V3": [("J1", 28), ("J1", 29), ("J2", 15), ("U1", 14), ("U2", 3),
                ("C1", 1), ("C4", 1), ("C5", 1), ("C6", 1)],
    "VCC_1V8": [("U2", 2), ("U1", 1), ("C2", 1), ("C3", 1), ("R1", 2)],
    "U1_OE": [("U1", 8), ("R1", 1)],
    "MIPI_D0_N": [("J1", 14), ("J2", 2)],
    "MIPI_D0_P": [("J1", 15), ("J2", 3)],
    "MIPI_D1_N": [("J1", 11), ("J2", 5)],
    "MIPI_D1_P": [("J1", 12), ("J2", 6)],
    "MIPI_CLK_N": [("J1", 17), ("J2", 8)],
    "MIPI_CLK_P": [("J1", 18), ("J2", 9)],
    # I2C / EN through the TXS0104E: A-side (1.8V) faces J1, B-side (3.3V)
    # faces J2 through 0R insurance resistors R2-R4.
    "SCL_1V8": [("J1", 24), ("U1", 2)],       # channel 1 A
    "SDA_1V8": [("J1", 25), ("U1", 3)],       # channel 2 A
    "CAM_EN_1V8": [("J1", 23), ("U1", 4)],    # channel 3 A
    "XVS_1V8": [("U1", 5), ("TP1", 1)],       # channel 4 A (spare sync)
    "SCL_LVL": [("U1", 13), ("R2", 1)],
    "SCL": [("R2", 2), ("J2", 13)],
    "SDA_LVL": [("U1", 12), ("R3", 1)],
    "SDA": [("R3", 2), ("J2", 14)],
    "CAM_EN_LVL": [("U1", 11), ("R4", 1)],
    "CAM_EN": [("R4", 2), ("J2", 11)],
    "XVS_3V3": [("U1", 10), ("TP2", 1)],      # channel 4 B (spare sync)
    # Optional reset breakout: JP1 open by default.
    "CM_RST_L": [("J1", 27), ("JP1", 1)],
    "RST_TP": [("JP1", 2), ("TP3", 1)],
}

# deliberately unconnected pins (get no-connect flags in the schematic)
NC_PINS = {
    "J1": [2, 3, 5, 6, 8, 9, 20, 22, 26, 30, 31],  # D3/D2/CLK1, CLKOUTs, PDN1, 5V
    "J2": [12],                                     # CAM_GPIO1
    "U1": [6, 9],                                   # TXS0104E NC pins
}

# Rock 5B+ CAM0 pin names (Radxa ROCK 5B+ V1.2 schematic p.18)
J1_PIN_NAMES = {
    1: "GND", 2: "CSI0_RX_D3N", 3: "CSI0_RX_D3P", 4: "GND",
    5: "CSI0_RX_D2N", 6: "CSI0_RX_D2P", 7: "GND",
    8: "CSI0_RX_CLK1N", 9: "CSI0_RX_CLK1P", 10: "GND",
    11: "CSI0_RX_D1N", 12: "CSI0_RX_D1P", 13: "GND",
    14: "CSI0_RX_D0N", 15: "CSI0_RX_D0P", 16: "GND",
    17: "CSI0_RX_CLK0N", 18: "CSI0_RX_CLK0P", 19: "GND",
    20: "CAM_CLKOUT0", 21: "GND", 22: "CAM_CLKOUT1",
    23: "CSI0_PDN0_H", 24: "I2C3_SCL_M0_MIPI", 25: "I2C3_SDA_M0_MIPI",
    26: "CSI0_PDN1", 27: "CM_RST_L_1", 28: "VCC_3V3", 29: "VCC_3V3",
    30: "VCC_5V", 31: "VCC_5V",
}

# RPi camera 15P pin names (official RPi camera connector pinout)
J2_PIN_NAMES = {
    1: "GND", 2: "CAM_D0_N", 3: "CAM_D0_P", 4: "GND",
    5: "CAM_D1_N", 6: "CAM_D1_P", 7: "GND",
    8: "CAM_CK_N", 9: "CAM_CK_P", 10: "GND",
    11: "CAM_GPIO0", 12: "CAM_GPIO1", 13: "SCL", 14: "SDA", 15: "3V3",
}

# ref -> (value, footprint lib_id, LCSC part or "")
PARTS = {
    "J1": ("Rock5B_CAM0_31P", "flexadapter:Flex_Finger_31P_0.3mm", ""),
    "J2": ("RPi_Cam_15P", "flexadapter:Flex_Finger_15P_1.0mm", ""),
    "U1": ("TXS0104EPWR", "flexadapter:TSSOP-14_4.4x5mm_P0.65mm", "C44955"),
    "U2": ("XC6206P182MR", "flexadapter:SOT-23-3", "C347373"),
    "C1": ("1uF", "flexadapter:C_0402_1005Metric", ""),
    "C2": ("1uF", "flexadapter:C_0402_1005Metric", ""),
    "C3": ("100nF", "flexadapter:C_0402_1005Metric", ""),
    "C4": ("100nF", "flexadapter:C_0402_1005Metric", ""),
    "C5": ("100nF", "flexadapter:C_0402_1005Metric", ""),
    "C6": ("4.7uF", "flexadapter:C_0603_1608Metric", ""),
    "R1": ("10k", "flexadapter:R_0402_1005Metric", ""),
    "R2": ("0R", "flexadapter:R_0402_1005Metric", ""),
    "R3": ("0R", "flexadapter:R_0402_1005Metric", ""),
    "R4": ("0R", "flexadapter:R_0402_1005Metric", ""),
    "TP1": ("XVS_1V8", "flexadapter:TestPoint_Pad_1.5x1.5mm", ""),
    "TP2": ("XVS_3V3", "flexadapter:TestPoint_Pad_1.5x1.5mm", ""),
    "TP3": ("RST_TP", "flexadapter:TestPoint_Pad_1.5x1.5mm", ""),
    "JP1": ("Open", "flexadapter:SolderJumper-2_P1.3mm_Open_RoundedPad1.0x1.5mm", ""),
}


def net_of(ref, pin):
    """Return the net name for (ref, pin) or None if unconnected."""
    for net, members in NETS.items():
        if (ref, pin) in members:
            return net
    return None


def sanity_check():
    """Every pin must be exactly once in NETS or NC_PINS."""
    seen = {}
    for net, members in NETS.items():
        for m in members:
            assert m not in seen, f"{m} in both {seen[m]} and {net}"
            seen[m] = net
    for ref, pins in NC_PINS.items():
        for p in pins:
            assert (ref, p) not in seen, f"({ref},{p}) is NC but also in net {seen.get((ref, p))}"
    for j, names, count in (("J1", J1_PIN_NAMES, 31), ("J2", J2_PIN_NAMES, 15)):
        for p in range(1, count + 1):
            assert (j, p) in seen or p in NC_PINS[j], f"({j},{p}) unaccounted"
    for p in range(1, 15):
        assert ("U1", p) in seen or p in NC_PINS["U1"], f"(U1,{p}) unaccounted"


if __name__ == "__main__":
    sanity_check()
    print("adapter_netlist: sanity check OK "
          f"({len(NETS)} nets, {sum(len(v) for v in NETS.values())} pins)")
