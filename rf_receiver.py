#!/usr/bin/env python3
"""
Detect 433/315 MHz OOK remotes with a cheap GPIO receiver on a Raspberry Pi,
using captures learned by a Broadlink RM (RM4 Pro / RM Pro) as reference.

Why this exists
---------------
Cheap superheterodyne/regen receivers (RFM210LH-D, XY-MK-5V, MX-RM-5V, ...)
output a continuous stream of *raw OOK edges* on their DATA pin and emit noise
when no carrier is present. There is no "code" to read back. This module:

  1. Reads a Broadlink RF capture (hex) and converts it to pulse durations.
  2. Derives a binary signature for PT2262 / EV1527-style frames (sync + 24
     short/long bit pairs), choosing the most-repeated frame by majority vote
     (a capture can start mid-transmission).
  3. Watches the GPIO receiver edges and matches incoming frames against the
     known signatures, tolerating dropped leading bits and noisy pulses.

So a remote learned once with a Broadlink can then be *detected* with a $2
receiver, and you can run any action when it fires.

Hardware
--------
  433 MHz receiver module (tested with a HopeRF RFM210LH; the very cheap
  XY-MK-5V / MX-RM-5V modules did not work in my testing)
    DOUT -> GPIO 27 (BCM, physical pin 13)   [--pin to change]
    VCC  -> 3.3V (pin 1)   <- do NOT feed 5V into a GPIO
    GND  -> GND  (pin 6)
    ANT  -> a single ~17 cm jumper/dupont wire (the RFM210 has a header pin
            for this, so no soldering needed)

Install
-------
  pip install pigpio broadlink
  sudo pigpiod      # start the pigpio helper (once per boot)

Self-test (no hardware, fakes the pulses in software)
-----------------------------------------------------
  python3 examples/offline_demo.py

Real usage
----------
  python3 rf_receiver.py --list                  # show known signals
  python3 rf_receiver.py                          # debug: print received frames
  python3 rf_receiver.py --daemon                 # run ACTIONS on known signals
  python3 rf_receiver.py --codes my_codes.json    # custom Broadlink capture file

codes.json format (as produced by Broadlink learn tools):
  { "device name": { "button name": "<broadlink hex packet>" }, ... }
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable

# --- Tunables -------------------------------------------------------------
GPIO_PIN        = 27        # DATA pin of the receiver module (BCM numbering)
GAP_US          = 120_000   # gap > this = packet separator
PREAMBLE_US     = 50_000    # pulses longer than this are preamble, dropped
TOLERANCE       = 0.40      # +/-40% when comparing pulse durations
MIN_PULSES      = 16        # minimum pulses before attempting a match
MIN_MATCH_RATIO = 0.74      # GPIO receiver may lose the start to AGC/noise
MIN_MATCH_MARGIN = 0.02     # minimum lead over the runner-up candidate
MAX_MATCH_OFFSET = 140      # search the useful frame inside the received packet
MIN_KNOWN_BITS  = 18        # avoid false positives when too many bits are '?'

DEFAULT_CODES = os.environ.get("RF_CODES", "codes.json")


# --- Actions (only used with --daemon) ------------------------------------
# Key: "device name/button name" exactly as in codes.json or BIT_PATTERNS.
# Value: a list of args for subprocess.Popen, or a Python callable.
ACTIONS: dict[str, list | Callable] = {
    # "doorbell/button":  ["python3", "/path/to/notify.py"],
    # "remote/A":         lambda: print("A pressed!"),
}

# Optional: bit signatures decoded directly from the GPIO receiver, useful when
# the waveform captured by Broadlink differs from the physical remote. Populated
# automatically from codes.json, but you can hardcode known ones here too:
#   "remote/A": "010111000100100011100001",
BIT_PATTERNS: dict[str, str] = {}


def load_rf_patterns(codes_path: Path) -> dict[str, list[int]]:
    """Read a Broadlink codes.json and return {key: [pulses_us]} for RF signals.

    Broadlink packet types 0xb1/0xb2/0xd7 are RF (433/315 MHz). IR (0x26) is
    ignored. Also fills BIT_PATTERNS with the derived PT2262/EV1527 signature.
    """
    if not codes_path.exists():
        print(f"WARNING: {codes_path} not found")
        return {}

    try:
        from broadlink.remote import data_to_pulses
    except ImportError:
        print("ERROR: pip install broadlink  (needed to parse codes.json)")
        sys.exit(1)

    raw = json.loads(codes_path.read_text())
    patterns = {}
    for device, buttons in raw.items():
        for button, hex_str in buttons.items():
            try:
                b = bytes.fromhex(hex_str)
                if b[0] in (0xb1, 0xb2, 0xd7):
                    pulses = data_to_pulses(b)
                    sub = _first_subpacket(pulses)
                    if len(sub) >= MIN_PULSES:
                        key = f"{device}/{button}"
                        patterns[key] = sub
                        bits = _extract_bit_pattern(pulses)
                        if bits:
                            BIT_PATTERNS[key] = bits
            except Exception as ex:
                print(f"WARNING: could not parse {device}/{button}: {ex}")
    return patterns


def synthesize_pulses(bits: str, sync_us: int = 9_000,
                      short_us: int = 300, long_us: int = 900) -> list[int]:
    """Build a PT2262/EV1527-style pulse train for a bit string.

    Handy for testing the decoder without any hardware. Each bit is encoded as
    a short/long pulse pair: 0 -> (short, long), 1 -> (long, short).
    """
    pulses = [sync_us]
    for bit in bits:
        if bit == "0":
            pulses += [short_us, long_us]
        else:
            pulses += [long_us, short_us]
    return pulses


def _first_subpacket(pulses: list[int]) -> list[int]:
    """Strip preamble and return the first sub-packet of bit pulses."""
    result = []
    in_packet = False
    for p in pulses:
        if p >= PREAMBLE_US:
            if in_packet and result:
                break
            in_packet = False
            result = []
        else:
            in_packet = True
            result.append(p)
    return result


def _match_score(received: list[int], stored: list[int], offset: int = 0) -> float:
    """Compare two pulse sequences by normalized ratio."""
    n = min(len(received) - offset, len(stored))
    if n < MIN_PULSES:
        return 0.0

    received = received[offset:offset + n]
    stored = stored[:n]

    comparable = [(r, s) for r, s in zip(received, stored) if r < 8_000 and s < 8_000]
    if len(comparable) < MIN_PULSES:
        return 0.0

    r_vals = sorted(r for r, _ in comparable)
    s_vals = sorted(s for _, s in comparable)
    n = len(comparable)
    med_r = r_vals[n // 2] or 1
    med_s = s_vals[n // 2] or 1

    matches = 0
    for r, s in comparable:
        if abs(r / med_r - s / med_s) <= TOLERANCE:
            matches += 1
    return matches / n


def _best_match(received: list[int], patterns: dict[str, list[int]]) -> tuple[str | None, float]:
    """Find the best matching pulse pattern, allowing an initial offset."""
    best_key = None
    best_score = 0.0
    second_score = 0.0

    max_offset = min(MAX_MATCH_OFFSET, max(0, len(received) - MIN_PULSES))
    for key, stored in patterns.items():
        key_best = 0.0
        for offset in range(max_offset + 1):
            key_best = max(key_best, _match_score(received, stored, offset))
        if key_best > best_score:
            second_score = best_score
            best_key = key
            best_score = key_best
        elif key_best > second_score:
            second_score = key_best

    if best_score >= MIN_MATCH_RATIO and (best_score - second_score) >= MIN_MATCH_MARGIN:
        return best_key, best_score
    return None, best_score


def _decode_frame_bits(frame: list[int]) -> str | None:
    """Decode a PT2262-like frame: sync + 24 short/long pulse pairs."""
    if len(frame) < 49 or not (4_000 <= frame[0] <= 15_000):
        return None

    bits = []
    for a, b in zip(frame[1:49:2], frame[2:49:2]):
        short = min(a, b)
        long = max(a, b)
        if short <= 0 or long / short < 1.5:
            bits.append("?")
        elif a < b:
            bits.append("0")
        else:
            bits.append("1")
    return "".join(bits)


def _extract_bit_pattern(pulses: list[int]) -> str | None:
    """Majority-vote the 24-bit frame from a Broadlink capture.

    A capture may begin mid-transmission; picking the most repeated frame avoids
    learning that initial fragment as if it were the real code.
    """
    frames = []
    for index, pulse in enumerate(pulses):
        if 4_000 <= pulse <= 15_000 and index + 49 <= len(pulses):
            bits = _decode_frame_bits(pulses[index:index + 49])
            if bits and "?" not in bits:
                frames.append(bits)
    return Counter(frames).most_common(1)[0][0] if frames else None


def _hamming(a: str, b: str) -> int:
    """Distance ignoring uncertain '?' bits from reception."""
    return sum(x != y for x, y in zip(a, b) if x != "?" and y != "?") + abs(len(a) - len(b))


def _known_bits(a: str, b: str) -> int:
    return sum(x != "?" and y != "?" for x, y in zip(a, b))


def _best_bit_match(received: list[int], patterns: dict[str, str]) -> tuple[str | None, int]:
    """Search decodable bit frames inside the received packet."""
    best_key = None
    best_distance = 999
    second_distance = 999

    for i, pulse in enumerate(received):
        if not (4_000 <= pulse <= 15_000) or i + 50 > len(received):
            continue
        bits = _decode_frame_bits(received[i:i + 50])
        if not bits or bits.count("?") > 6:
            continue
        for key, pattern in patterns.items():
            if _known_bits(bits, pattern) < MIN_KNOWN_BITS:
                continue
            distance = _hamming(bits, pattern)
            if distance < best_distance:
                second_distance = best_distance
                best_key = key
                best_distance = distance
            elif distance < second_distance:
                second_distance = distance

    if best_distance <= 2 and best_distance < second_distance:
        return best_key, best_distance
    return None, best_distance


def _match_decoded_bits(bits: str, patterns: dict[str, str]) -> tuple[str | None, int]:
    """Compare an already-decoded frame against known signatures."""
    best_key = None
    best_distance = 999
    second_distance = 999
    for key, pattern in patterns.items():
        if _known_bits(bits, pattern) < MIN_KNOWN_BITS:
            continue
        distance = _hamming(bits, pattern)
        if distance < best_distance:
            second_distance = best_distance
            best_key = key
            best_distance = distance
        elif distance < second_distance:
            second_distance = distance
    if best_distance <= 2 and best_distance < second_distance:
        return best_key, best_distance
    return None, best_distance


class RFReceiver:
    """Listens to a GPIO receiver and fires callbacks on recognized frames."""

    def __init__(self, gpio_pin: int):
        try:
            import pigpio
        except ImportError:
            print("ERROR: pip install pigpio  &&  sudo systemctl start pigpiod")
            sys.exit(1)

        self.pigpio = pigpio
        self.gpio_pin = gpio_pin
        self.pi = pigpio.pi()
        if not self.pi.connected:
            print("ERROR: pigpiod not running. Run: sudo systemctl start pigpiod")
            sys.exit(1)

        self._pulses: list[int] = []
        self._last_tick: int = 0
        self._frame_candidate: list[int] = []

        self.patterns: dict[str, list[int]] = {}
        self.bit_patterns: dict[str, str] = BIT_PATTERNS
        self.on_match: Callable | None = None
        self.on_frame: Callable | None = None

        self.pi.set_mode(gpio_pin, pigpio.INPUT)

    def start(self):
        self._cb = self.pi.callback(
            self.gpio_pin, self.pigpio.EITHER_EDGE, self._edge_callback
        )

    def stop(self):
        if getattr(self, "_cb", None):
            self._cb.cancel()
        self.pi.stop()

    def _edge_callback(self, gpio, level, tick):
        if self._last_tick == 0:
            self._last_tick = tick
            return
        duration_us = self.pigpio.tickDiff(self._last_tick, tick)
        self._last_tick = tick

        if duration_us > 2_000_000:
            self._pulses = []
        elif duration_us > GAP_US and self._pulses:
            self._try_match()
            self._pulses = []
        else:
            self._pulses.append(duration_us)

        self._feed_frame_decoder(duration_us)

    def _feed_frame_decoder(self, duration_us: int):
        """Find sync + 24 pulse pairs even when DOUT has continuous noise."""
        if 4_000 <= duration_us <= 15_000:
            self._frame_candidate = [duration_us]
            return
        if not self._frame_candidate:
            return
        self._frame_candidate.append(duration_us)
        if len(self._frame_candidate) < 49:
            return

        frame = self._frame_candidate
        self._frame_candidate = []
        bits = _decode_frame_bits(frame)
        if not bits or bits.count("?") > 6:
            return

        key, _ = _match_decoded_bits(bits, self.bit_patterns)
        if key and self.on_match:
            self.on_match(key, frame)
        elif self.on_frame:
            self.on_frame(bits, frame)

    def _try_match(self):
        sub = _first_subpacket(self._pulses)
        if len(self._pulses) < MIN_PULSES:
            return

        key, _ = _best_bit_match(self._pulses, self.bit_patterns)
        if key:
            if self.on_match:
                self.on_match(key, sub)
            return

        # DOUT can contain thousands of noise edges before the packet. A
        # Broadlink RF capture ends immediately before its long final gap, so
        # compare against only the recent window sized to the longest pattern.
        max_len = max((len(p) for p in self.patterns.values()), default=0)
        recent = self._pulses[-max_len:] if max_len else self._pulses
        key, _ = _best_match(recent, self.patterns)
        if key and self.on_match:
            self.on_match(key, recent)


def main():
    parser = argparse.ArgumentParser(
        description="433 MHz receiver -- match GPIO frames against Broadlink captures"
    )
    parser.add_argument("--daemon", action="store_true", help="run ACTIONS on known signals")
    parser.add_argument("--list", action="store_true", help="list loaded RF signals")
    parser.add_argument("--pin", type=int, default=GPIO_PIN, help=f"GPIO BCM pin (default {GPIO_PIN})")
    parser.add_argument("--codes", default=DEFAULT_CODES, help=f"Broadlink codes.json (default {DEFAULT_CODES})")
    args = parser.parse_args()

    patterns = load_rf_patterns(Path(args.codes))

    if args.list:
        if not patterns:
            print("No RF signals loaded.")
            return
        print(f"{'Signal':<40} {'Pulses':>6}  {'Derived bits':<24}  Action")
        print("-" * 90)
        for key, pulses in patterns.items():
            action = "-> " + str(ACTIONS[key]) if key in ACTIONS else "(no action)"
            print(f"{key:<40} {len(pulses):>6}  {BIT_PATTERNS.get(key, '-'):<24}  {action}")
        return

    print(f"Loaded {len(patterns)} RF signals from {args.codes}")
    print(f"GPIO pin: BCM {args.pin}  |  Ctrl+C to exit")
    print(f"Mode: {'daemon (' + str(len(ACTIONS)) + ' actions)' if args.daemon else 'debug (printing frames)'}")

    rx = RFReceiver(args.pin)
    rx.patterns = patterns

    last_fired: dict[str, float] = {}
    last_frame: dict[str, float] = {}
    DEBOUNCE_S = 0.5

    def on_match(key: str, pulses: list[int]):
        now = time.monotonic()
        if now - last_fired.get(key, 0) < DEBOUNCE_S:
            return
        last_fired[key] = now
        print(f"[{time.strftime('%H:%M:%S')}] Detected: {key}  ({len(pulses)} pulses)")
        if args.daemon and key in ACTIONS:
            action = ACTIONS[key]
            print(f"  -> running: {action}")
            action() if callable(action) else subprocess.Popen(action)

    def on_frame(bits: str, pulses: list[int]):
        if args.daemon:
            return
        now = time.monotonic()
        if now - last_frame.get(bits, 0) < DEBOUNCE_S:
            return
        last_frame[bits] = now
        print(f"[{time.strftime('%H:%M:%S')}] Unknown RF frame: {bits}  ({len(pulses)} pulses)")

    rx.on_match = on_match
    rx.on_frame = on_frame
    rx.start()

    def shutdown(sig=None, frame=None):
        print("\nExiting.")
        rx.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
