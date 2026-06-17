# broadlink-rf-gpio-receiver

Detect cheap **433/315 MHz OOK remotes, doorbells and sensors** with a $2 GPIO
receiver on a Raspberry Pi — using signals you already learned with a
**Broadlink RM** as the reference.

A Broadlink RM4 Pro can *learn and replay* RF, but it can't tell you when a
button is pressed. A cheap receiver module can hear the button, but only gives
you a noisy stream of raw edges with no "code" to read. This bridges the two:
it turns a Broadlink capture into a bit signature and matches it live against
the receiver's edges, so a learned remote becomes an **event you can act on**.

Handy for: doorbell → phone notification, extra remote buttons → run a script,
PIR/door sensors → home automation, etc.

## How it works

```
Broadlink capture (hex)  ──►  pulses  ──►  24-bit signature (PT2262/EV1527)
                                                     │
433 MHz receiver on GPIO  ──►  raw edges  ──►  match ┘  ──►  on_match(key) ──► action
```

The matcher tolerates the realities of cheap receivers: continuous noise at
rest, AGC garbage before the packet, dropped leading bits, and per-edge timing
jitter. See [`docs/broadlink_to_gpio.md`](docs/broadlink_to_gpio.md) for the
full decoding pipeline.

## Try it in 5 seconds (no hardware)

```bash
python3 examples/offline_demo.py
```

It synthesizes two fake remotes, decodes them back to bits, then recognizes a
noisy, partial reception of one of them — the exact path the GPIO receiver uses
live.

## Hardware

```
433 MHz receiver module (RFM210LH-D, XY-MK-5V, MX-RM-5V, ...)
  DATA  -> GPIO 27   (BCM, physical pin 13)     # change with --pin
  VCC   -> 3.3V      (pin 1)   <- do NOT feed 5V into a GPIO
  GND   -> GND       (pin 6)
```

Solder a ~17 cm straight wire to the module's antenna pad for usable range.

## Install

```bash
pip install pigpio broadlink
sudo systemctl enable --now pigpiod
```

## Usage

```bash
# Inspect a Broadlink codes.json and the derived signatures
python3 rf_code_info.py --codes codes.example.json

# List loaded signals
python3 rf_receiver.py --list --codes codes.example.json

# Debug: print every frame the receiver decodes (find/learn new remotes)
python3 rf_receiver.py --codes codes.json

# Daemon: run an action when a known signal is detected
python3 rf_receiver.py --daemon --codes codes.json
```

`codes.json` is whatever your Broadlink learn tool produces:

```json
{ "device name": { "button name": "<broadlink hex packet>" } }
```

(`codes.example.json` is a synthetic file so the commands above work out of the
box.) Wire up reactions by editing the `ACTIONS` dict at the top of
`rf_receiver.py` — values can be a `subprocess` arg list or a Python callable.

## Example: doorbell → notification

```python
# rf_receiver.py
ACTIONS = {
    "doorbell/button": ["python3", "/home/pi/notify.py", "Someone's at the door"],
}
```

```bash
python3 rf_receiver.py --daemon
```

## Notes

- The receiver emits noise continuously; never treat raw GPIO activity as a
  press. Act only on `on_match`.
- Works with PT2262/EV1527-style fixed-code remotes (most cheap ones). Rolling
  codes are intentionally not matchable.
- Real remotes repeat the code several times per press; the matcher uses that.

## License

MIT — see [LICENSE](LICENSE).
