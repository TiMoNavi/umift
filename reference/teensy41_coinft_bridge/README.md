# Teensy 4.1 CoinFT bridge reference

This directory is historical reference material. The runnable Teensy 4.1
project is maintained in:

```text
modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware/
```

Use that project and its matching smoke test for current work. It supports
the documented 0/1/2 sensor behavior:

- 0 sensors: `m` emits simulated dual-sensor packets for transport testing.
- 1 sensor: `s` emits the single sensor's 24-byte frame body for personal
  debugging. This mode is not the production capture protocol.
- 2 sensors: `s` emits the production 58-byte packet.

The production packet is:

```text
0x00 0x00
+ sequence_id uint32 little-endian
+ teensy_time_us uint32 little-endian
+ left 24-byte CoinFT frame body
+ right 24-byte CoinFT frame body
```

The higher-numbered active Teensy UART is exported as `left`; the lower one
is exported as `right`. The current calibration mapping is recorded in
`modules/03_coinft_teensy/assets/calibration/production/current_left_right_mapping.json`:

```text
left  = coinft_2606602_0019
right = coinft_2606602_0012
```

## Build and flash

```bash
cd "$UMIFT_ROOT/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware"
pio run
pio run -t upload
```

Install PlatformIO with `python3 -m pip install platformio` if it is not
available. Press the Teensy 4.1 `Program` button when the uploader requests it.

## Host smoke test

```bash
python3 "$UMIFT_ROOT/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/tools/coinft_teensy_smoke_test.py" \
  --port /dev/cu.usbmodemXXXX
```

The smoke test verifies framing and raw channels only; ONNX calibration runs
on the host through Module 04.

## Historical source note

The original recovered Teensy 4.0 source is not a runtime dependency and is
not required to build this package. Keep this directory as a protocol note;
do not copy files from an external recovery checkout into a fresh delivery.
