# UMIFTiPhoneCaptureCore

This is the clean app line for the final iPhone capture workflow.

Legacy prototypes and verification apps live under `../legacy/`. New cleanup,
module boundary work, and future recording/streaming work should happen here,
not in the archived app directories.

## Current Runtime Direction

The app uses one long-lived ARKit owner:

```text
ARKit WorldTracking
  -> wide RGB / depth / intrinsics / 6DoF pose
  -> private ultrawide side stream for gripper marker vision
```

The older `AVCaptureMultiCamSession` route is kept only as reference code under
`App/LegacyCapture`. It should not become the main capture owner again.

## Module Layout

```text
App/
  AppLifecycle/      SwiftUI app entry and orientation lock
  AppState/          app phase, calibration state, public UI state
  ARCapture/         ARKit frame ingestion and private ultrawide dispatch
  Calibration/       world-origin AR view and motion provider
  Diagnostics/       debug automation flags and future logging utilities
  GripperVision/     marker detection, path calibration, opening estimation
  LegacyCapture/     old AVCapture code kept for reference during migration
  Recording/         local JSONL recording, stream schema, live TCP JSONL server
  UI/                dashboard, preview pane, controls, parameter panel
```

Desktop helpers live outside the app under:

```text
modules/04_laptop_alignment_export/
  entrypoints/iphone_stream_receiver.py
                             Protocol V2 H.264 archive receiver
```

## Build

```sh
DEVELOPER_DIR=/Users/550m/Downloads/Xcode.app/Contents/Developer \
xcodebuild \
  -project UMIFTiPhoneCaptureCore.xcodeproj \
  -scheme UMIFTiPhoneCaptureCore \
  -configuration Debug \
  -destination 'id=00008130-000E2DA10141001C' \
  -allowProvisioningUpdates \
  build
```

## Next Cleanup Steps

1. Move torch ownership out of `ARCaptureModel`.
2. Move pose text formatting out of `CaptureAppState`.
3. Split calibration flow side effects out of `CaptureDashboardView`.
4. Split gripper path calibration storage and opening estimation out of
   `GripperVisionProcessor`.
5. Add desktop timebase synchronization for the recording stream.
