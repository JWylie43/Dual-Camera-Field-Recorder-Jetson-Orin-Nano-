#!/usr/bin/env python3
"""Assert the exported KiCad netlist matches the engineering spec table.

Parses ../rock5_cam_flex.net (kicadsexpr netlist, regenerate with:
  kicad-cli sch export netlist --format kicadsexpr \
      --output rock5_cam_flex.net rock5_cam_flex.kicad_sch)
and verifies every row of the spec's net table pin-for-pin, including the
level-translator channel mapping and the pins that MUST stay unconnected
(J1.30/31 = 5V). Exits non-zero on any mismatch.

The table below is written out explicitly from the spec (not imported from
adapter_netlist.py) so this check is independent of the generators.
"""
import os
import re
import sys

NETLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "rock5_cam_flex.net")

# --- minimal s-expression parser -------------------------------------------

def parse_sexp(text):
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()"]+', text)
    pos = 0

    def walk():
        nonlocal pos
        node = []
        while pos < len(tokens):
            t = tokens[pos]
            pos += 1
            if t == "(":
                node.append(walk())
            elif t == ")":
                return node
            else:
                node.append(t.strip('"'))
        return node

    assert tokens[0] == "("
    pos = 1
    return walk()


def find_all(node, tag):
    out = []
    for item in node:
        if isinstance(item, list):
            if item and item[0] == tag:
                out.append(item)
            out.extend(find_all(item, tag))
    return out


def get(node, tag):
    for item in node:
        if isinstance(item, list) and item and item[0] == tag:
            return item
    return None


# --- load nets ---------------------------------------------------------------

def load_nets(path):
    """Return {netname: frozenset((ref, pin))} and {(ref,pin): netname}."""
    root = parse_sexp(open(path).read())
    nets = {}
    pin2net = {}
    for net in find_all(root, "net"):
        name = get(net, "name")[1]
        name = name.lstrip("/")  # local labels are exported as /NAME
        members = set()
        for node in find_all(net, "node"):
            ref = get(node, "ref")[1]
            pin = get(node, "pin")[1]
            members.add((ref, pin))
            pin2net[(ref, pin)] = name
        nets[name] = frozenset(members)
    return nets, pin2net


# --- spec table (authoritative) ----------------------------------------------

failures = []


def check(cond, msg):
    if cond:
        print(f"  OK   {msg}")
    else:
        print(f"  FAIL {msg}")
        failures.append(msg)


def main():
    nets, pin2net = load_nets(NETLIST)

    def same_net(a, b):
        return (a in pin2net and b in pin2net
                and pin2net[a] == pin2net[b])

    print("== direct nets (no translator) ==")
    # GND
    gnd_expected = {("J1", p) for p in "1 4 7 10 13 16 19 21".split()} | \
                   {("J2", p) for p in "1 4 7 10".split()}
    gnd = nets.get("GND", frozenset())
    check(gnd_expected <= gnd, f"GND contains J1 1,4,7,10,13,16,19,21 + J2 1,4,7,10")

    # MIPI diff pairs: J1 pin <-> J2 pin, direct
    for net, j1, j2 in (("MIPI_D0_N", "14", "2"), ("MIPI_D0_P", "15", "3"),
                        ("MIPI_D1_N", "11", "5"), ("MIPI_D1_P", "12", "6"),
                        ("MIPI_CLK_N", "17", "8"), ("MIPI_CLK_P", "18", "9")):
        check(same_net(("J1", j1), ("J2", j2)),
              f"{net}: J1.{j1} <-> J2.{j2} direct")
        check(len(nets.get(pin2net.get(("J1", j1), ""), ())) == 2,
              f"{net}: exactly 2 nodes (no taps on the diff pair)")

    # 3.3V direct
    check(same_net(("J1", "28"), ("J2", "15")) and same_net(("J1", "29"), ("J2", "15")),
          "VCC_3V3: J1.28,29 <-> J2.15 direct")

    print("== translator channels (TXS0104E: A=1.8V Rock side, B=3.3V camera side) ==")
    # channel: (name, J1 pin, A pin, B pin, J2 pin, series R or None)
    channels = (("SCL   (ch1)", "24", "2", "13", "13", "R2"),
                ("SDA   (ch2)", "25", "3", "12", "14", "R3"),
                ("CAM_EN(ch3)", "23", "4", "11", "11", "R4"))
    for name, j1p, apin, bpin, j2p, r in channels:
        check(same_net(("J1", j1p), ("U1", apin)),
              f"{name}: J1.{j1p} <-> U1.A (pin {apin})")
        check(same_net(("U1", bpin), (r, "1")),
              f"{name}: U1.B (pin {bpin}) <-> {r}.1 (0R insurance)")
        check(same_net((r, "2"), ("J2", j2p)),
              f"{name}: {r}.2 <-> J2.{j2p}")

    # channel 4: XVS spare, test pads only
    check(same_net(("U1", "5"), ("TP1", "1")), "XVS   (ch4): U1.A4 (pin 5) <-> TP1")
    check(same_net(("U1", "10"), ("TP2", "1")), "XVS   (ch4): U1.B4 (pin 10) <-> TP2")

    print("== translator power ==")
    check(same_net(("U1", "1"), ("U2", "2")), "U1.VCCA (pin 1) fed by U2.VOUT (1.8V)")
    check(pin2net.get(("U1", "14")) == pin2net.get(("J2", "15")),
          "U1.VCCB (pin 14) on VCC_3V3")
    check(same_net(("U2", "3"), ("J1", "28")), "U2.VIN on VCC_3V3")
    check(same_net(("U1", "8"), ("R1", "1")) and same_net(("R1", "2"), ("U1", "1")),
          "U1.OE pulled to VCCA via R1")

    print("== reset breakout (optional) ==")
    check(same_net(("J1", "27"), ("JP1", "1")), "J1.27 CM_RST_L_1 <-> JP1.1")
    check(same_net(("JP1", "2"), ("TP3", "1")), "JP1.2 <-> TP3")

    print("== pins that MUST be unconnected ==")
    for ref, pins in (("J1", "2 3 5 6 8 9 20 22 26 30 31".split()),
                      ("J2", ["12"])):
        for p in pins:
            crit = " (5V - safety critical)" if (ref, p) in (("J1", "30"), ("J1", "31")) else ""
            # KiCad exports no-connect pins as single-node "unconnected-..." nets
            netname = pin2net.get((ref, p))
            is_open = (netname is None
                       or netname.startswith("unconnected-")
                       or len(nets[netname]) == 1)
            check(is_open, f"{ref}.{p} unconnected{crit}")

    print()
    if failures:
        print(f"NETLIST CHECK FAILED: {len(failures)} mismatch(es)")
        sys.exit(1)
    print("NETLIST CHECK PASSED: every spec-table row verified.")


if __name__ == "__main__":
    main()
