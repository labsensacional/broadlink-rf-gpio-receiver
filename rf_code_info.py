#!/usr/bin/env python3
"""Show Broadlink RF packets and their derived GPIO-receiver bit signatures.

  python3 rf_code_info.py                      # all RF codes in codes.json
  python3 rf_code_info.py "remote" A           # one device/button
  python3 rf_code_info.py --codes my.json
"""
import argparse
import json
import os
from pathlib import Path

from broadlink.remote import data_to_pulses

from rf_receiver import _extract_bit_pattern


def main():
    parser = argparse.ArgumentParser(description="Inspect Broadlink RF codes and derived receiver signatures")
    parser.add_argument("device", nargs="?")
    parser.add_argument("button", nargs="?")
    parser.add_argument("--codes", default=os.environ.get("RF_CODES", "codes.json"))
    args = parser.parse_args()

    codes = json.loads(Path(args.codes).read_text())
    rows = []
    for device, buttons in codes.items():
        if args.device and device != args.device:
            continue
        for button, hex_code in buttons.items():
            if args.button and button != args.button:
                continue
            packet = bytes.fromhex(hex_code)
            if not packet or packet[0] not in (0xB1, 0xB2, 0xD7):
                continue
            pulses = data_to_pulses(packet)
            rows.append((f"{device}/{button}", f"0x{packet[0]:02x}", len(pulses),
                         _extract_bit_pattern(pulses) or "-"))

    if not rows:
        raise SystemExit("No matching RF codes found.")

    print(f"{'Signal':<40} {'Type':<6} {'Pulses':>7}  Derived bits")
    print("-" * 84)
    for key, packet_type, pulse_count, bits in rows:
        print(f"{key:<40} {packet_type:<6} {pulse_count:>7}  {bits}")


if __name__ == "__main__":
    main()
