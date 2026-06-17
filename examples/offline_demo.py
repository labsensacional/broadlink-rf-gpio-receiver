#!/usr/bin/env python3
"""
Hello world -- runs with NO hardware and NO Broadlink installed.

It synthesizes the OOK pulse train of a couple of fake 24-bit remotes, decodes
them back to bits (exactly what the GPIO receiver path does), then proves the
matcher recognizes a noisy, partially-dropped reception of one of them.

    python3 examples/offline_demo.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rf_receiver import (  # noqa: E402
    _extract_bit_pattern,
    _best_bit_match,
    synthesize_pulses,
)

# Two made-up remotes (PT2262/EV1527 send a fixed 24-bit code per button).
REMOTES = {
    "remote/A": "010111000100100011100001",
    "remote/B": "010111000100100011100010",
}


def add_noise(pulses, jitter=0.12, drop_leading=6):
    """Mimic a real receiver: AGC garbage up front + per-edge timing jitter."""
    noise = [random.randint(150, 1200) for _ in range(drop_leading)]
    jittered = [max(1, int(p * (1 + random.uniform(-jitter, jitter)))) for p in pulses]
    return noise + jittered


def main():
    random.seed(42)

    print("1) Encode -> decode round trip\n")
    for key, bits in REMOTES.items():
        pulses = synthesize_pulses(bits)
        decoded = _extract_bit_pattern(pulses)
        ok = "OK" if decoded == bits else "MISMATCH"
        print(f"   {key:<12} {bits}  ->  {decoded}  [{ok}]")

    print("\n2) Match a noisy, partial reception of 'remote/A'\n")
    # Real remotes transmit the code several times in a row, separated by gaps.
    frame = synthesize_pulses(REMOTES["remote/A"])
    received = add_noise(frame + [60_000] + frame)
    print(f"   received {len(received)} raw edges (with leading noise + jitter)")

    key, distance = _best_bit_match(received, REMOTES)
    if key:
        print(f"   -> detected: {key}  (hamming distance {distance})")
    else:
        print("   -> no confident match")

    print("\nNo hardware required. On a Raspberry Pi, rf_receiver.py does this")
    print("live against the edges coming from a 433 MHz receiver on a GPIO pin.")


if __name__ == "__main__":
    main()
