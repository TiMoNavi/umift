# CaptureCore Module Boundaries

This document defines the intended ownership boundaries for the clean app.
Keep behavior-preserving refactors aligned with these boundaries.

## AppLifecycle

Owns only process and scene startup:

- SwiftUI `@main` entry.
- Landscape orientation lock.
- Top-level environment object construction.

It should not contain capture logic, calibration logic, or diagnostics toggles.

## AppState

Owns user-visible state and phase transitions:

- preview / gripper calibration / world calibration / ready / countdown / recording
- public state shown by the parameter panel
- event log
- high-level transition commands such as `beginCalibration()`

It may transform incoming states into display state, but it should not own
camera sessions, OpenCV processing, or private ARFrame selectors.

## ARCapture

Owns ARKit frame ingestion and derived capture streams:

- wide timestamp and frame summary
- private ultrawide side-stream dispatch
- wide/ultrawide sync status
- gripper frame handoff

Future target: split hardware helpers out:

- `TorchController`
- `PrivateUltraWideFrameExtractor`
- `FrameSyncBuffer`

## Calibration

Owns world calibration mechanics:

- ARSCNView / ARSession rendering
- world-origin hit test
- world-origin anchor visualization
- AR frame state emitted to AppState
- device motion provider

It should not decide the whole app workflow. Workflow decisions belong in
`AppState` or a future `CalibrationCoordinator`.

## GripperVision

Owns all marker and gripper math:

- OpenCV bridge input and output parsing
- marker candidate filtering
- marker overlays
- one-dimensional gripper path calibration
- opening 0-100 estimation
- confidence estimation

Future target: split `GripperVisionProcessor` into:

- `MarkerDetector`
- `MarkerCandidateTracker`
- `GripperPathCalibrationStore`
- `GripperOpeningEstimator`
- `MarkerOverlayBuilder`

## UI

Owns presentation and user actions:

- preview pane
- marker overlays
- parameter panel
- calibration and recording buttons

UI may call high-level commands, but should not start/stop calibration services
directly after the flow coordinator exists.

## Recording

Owns the recording-mode contract and transport-facing data shapes:

- recording phase model and receiver/transport status
- fixed transmit schema
- cross-device timestamp contract
- conversion boundary between rich internal capture state and narrow send frames
- 3-second rolling in-memory frame window
- packet preview generation for on-device diagnostics

The authoritative transport is Protocol V2: one `1920x1440@30` hardware H.264
stream with frame metadata, depth and recording events sharing the same
sequence. JPEG/MJPEG packet transport is retired and must not be reintroduced.

Recording also owns clock-sync request handling. It replies with iPhone receive
and send uptime timestamps without entering the video spool or consuming a video
sequence number. The Mac combines those values with its send/receive timestamps
to fit the cross-device affine clock model.

The rolling buffer must not retain ARKit-owned `CVPixelBuffer` references.
Those buffers belong to the camera pipeline; keeping them for seconds can stall
the private ultrawide side stream. RGB should be copied or encoded into app-owned
`Data` at a dedicated transport/encoder boundary.

## Diagnostics

Owns debug-only policy and observability:

- launch automation flags
- watchdog constants
- structured debug log helpers

Diagnostics must be easy to disable for production capture.

## LegacyCapture

Contains the old `AVCaptureMultiCamSession` implementation for reference only.
It should not be used as the primary runtime capture owner in CaptureCore.
