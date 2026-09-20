# CoinFT Calibration Assets

This folder contains the calibration assets used to convert CoinFT raw 12-channel data into 6-axis force/torque output.

## Current Production Assets

Current physical mapping:

```text
left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
right = lower numbered active Teensy Serial/pins  = coinft_2606602_0012
```

This matches the current Teensy firmware rule:

```text
USB packet payload = left raw 12ch + right raw 12ch
left  comes from the higher numbered active Teensy Serial/pins
right comes from the lower numbered active Teensy Serial/pins
```

Use this file for the active left/right mapping:

- [production/current_left_right_mapping.json](production/current_left_right_mapping.json)

## Files That Matter At Runtime

For each physical CoinFT, the runtime-critical files are:

```text
*_MLP.onnx
CFT24_MLP.onnx.data
*_norm.json
manifest.json
```

Important: the exported ONNX files reference external tensor data named `CFT24_MLP.onnx.data`, so each model directory keeps that compatibility filename next to the ONNX file.

## Training / Audit Files

These are useful for traceability, but not needed for normal runtime inference:

```text
*_MLP.pth
*_MLP_results.png
source_README.md
```

Raw HDF5 calibration captures and many intermediate plots were intentionally not copied here. They remain in:

```text
/Users/550m/Downloads/05_testing_calibration_acceptance/coin-ft/device_models/
```

## Not Current / Legacy

`coinft_2606602_0008` is retained for historical comparison only. The current left/right inference pair is `0019` / `0012`, as recorded in `production/current_left_right_mapping.json`.
