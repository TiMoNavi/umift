# CoinFT to Module 04 integration

Module 03 provides raw CoinFT frames through the Teensy 4.1 USB serial bridge.
Module 04 reads the 58-byte dual-sensor packet and applies the selected ONNX
and norm files on the host.

## Current production configuration

Use the package-local configuration:

```bash
export UMIFT_ROOT="$(pwd)"
export UMIFT_COINFT_CONFIG="$UMIFT_ROOT/modules/03_coinft_teensy/configs/coinft_04_laptop_calibrated.json"
```

The configuration resolves the two current production assets:

```text
left  = coinft_2606602_0019
right = coinft_2606602_0012
```

The physical wiring must follow the same mapping because CoinFT boards do not
provide an electronic identity in the UART payload.

## Start Module 04

```bash
cd "$UMIFT_ROOT/modules/04_laptop_alignment_export"
python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8767 \
  --no-open \
  --no-auto-start \
  --coinft-config "$UMIFT_COINFT_CONFIG"
```

Before starting a real capture, run the Module 03 smoke test with `--simulate`
or connect both CoinFT boards and verify that sequence IDs and both 12-channel
raw payloads advance.
