# CoinFT firmware and calibration reference

This directory records historical provenance. It is not a runtime dependency.
All files required by the current package are already under
`modules/03_coinft_teensy/`.

## Included runnable assets

- CoinFT sensor-board firmware image:
  `modules/03_coinft_teensy/steps/01_coinft_sensor_firmware_flash/assets/CoinFT_V2_firmware.hex`
- Teensy 4.1 bridge source:
  `modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware/`
- Current calibrated ONNX and norm files:
  `modules/03_coinft_teensy/assets/calibration/production/`

The `.hex` file is for the CoinFT sensor board, not the Teensy. Do not flash
it to the Teensy. The Teensy bridge is built from the package-local PlatformIO
project and emits the 58-byte dual-sensor protocol.

Older recovered model names such as `UFT3_MLP_4L_scl_1_30.onnx` and
`UFT4_MLP_4L_scl_1_30.onnx` are provenance notes only; they are not present in
this delivery and must not be selected by a runtime configuration.
