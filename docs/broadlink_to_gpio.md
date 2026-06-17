# Broadlink RF capture → GPIO receiver signature

## Goal

Use RF signals **learned once with a Broadlink RM** (RM4 Pro / RM Pro) as
reference patterns to **detect** the same remote, doorbell, or sensor with a
cheap 433/315 MHz receiver wired to a Raspberry Pi GPIO pin.

There is no conversion of the Broadlink hex into a "module code". The cheap
receiver hands you the raw OOK pulses on its DATA pin. The software converts the
Broadlink packet into pulses and derives a binary signature that can be compared
against those received pulses.

## Pipeline

1. Learn the signal with a Broadlink tool; it is stored as a hex string in a
   `codes.json` of the form `{"device": {"button": "<hex>"}}`.
2. `broadlink.remote.data_to_pulses()` converts the packet into microsecond
   durations.
3. Find a sync pulse between 4 and 15 ms.
4. After the sync, read 24 pulse pairs:
   - short then long = `0`
   - long then short = `1`
5. A Broadlink capture can start mid-transmission. All repetitions are decoded
   and the **most frequent** 24-bit frame is chosen by majority vote.
6. On reception, degraded pairs are marked `?`. The matcher ignores those bits,
   requires at least 18 known bits, and rejects ties between buttons.

This covers PT2262 / EV1527-style protocols (most cheap remotes and doorbells).
For other protocols, a secondary matcher compares against the full sequence of
Broadlink-converted pulses.

## Inspect conversions

```bash
python3 rf_code_info.py --codes codes.example.json
python3 rf_code_info.py "example remote" A --codes codes.example.json
```

## Learn and detect a new device

1. Learn the button with any Broadlink learn tool and add it to `codes.json`.
2. Inspect the derived signature:

   ```bash
   python3 rf_code_info.py "device name" "button"
   ```

3. Listen and confirm it is detected:

   ```bash
   python3 rf_receiver.py --pin 27 --codes codes.json
   ```

If the protocol is sync + 24 bits (PT2262/EV1527) a signature appears in
`rf_code_info.py` and it can be detected directly. For other protocols the
receiver keeps a secondary matcher against the full converted pulse sequence.

## Relevant API (`rf_receiver.py`)

- `load_rf_patterns(path)` — loads `codes.json`, converts pulses, derives bits.
- `synthesize_pulses(bits)` — build a test pulse train (no hardware).
- `_extract_bit_pattern(pulses)` — majority-vote the repeated frame.
- `_decode_frame_bits(frame)` — sync + pulse pairs → bits.
- `RFReceiver.on_match(key, pulses)` — fired when a known signal is recognized.
- `RFReceiver.on_frame(bits, pulses)` — unknown frames, in debug mode.

The receiver module produces continuous noise at rest. Do not treat GPIO
activity as a button press; always act on frames recognized by `on_match`.
