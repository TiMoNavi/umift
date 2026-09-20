# Recording Mode

## Current Behavior

The app now separates live streaming from valid recording.

- The TCP JSONL server is open while the app is running.
- During calibration/debug phases, no capture frames are streamed.
- Normal preview phases continuously stream wide RGB + pose/gripper validity
  state to the Mac when a receiver is connected. This includes uncalibrated
  preview; unavailable pose/gripper values are marked invalid.
- Pressing record sends a `record_start` event and sets the recording flag on
  subsequent frames. The Mac treats frames after that event as the valid demo.
- Stopping sends `record_stop`; frames outside the marked segment remain useful
  for preview, health checks, and timestamp alignment, but are not the demo.
- Recording does not control whether the iPhone streams. It only marks the stream
  segment that the Mac should keep as valid capture data.

What it records now:

- `record_countdown_started`
- `recording_started`
- `stop_reason=screen_terminate_button`
- UI phase changes: `previewCalibrated -> startCountdown -> recording`
- live debug state shown on screen
- a rolling 3-second in-memory window of aligned wide-frame samples
- each wide-frame sample keeps main camera RGB metadata
- latest gripper opening is paired to each wide-frame sample when it is recent
- packet preview fields are shown in the right-side advanced panel
- when recording is active, lightweight V1 debug rows are written to
  `Documents/Recordings/*.jsonl`

What it sends now:

- A TCP JSONL stream listens on port `17381`.
- The Mac-side receiver lives in `modules/04_laptop_alignment_export/` and
  connects directly to the app port through usbmux.
- The receiver saves both raw JSONL and Mac-time-aligned JSONL.
- The receiver also writes `recording_only_iphone_stream.jsonl`, containing only
  `record_start` / `record_stop` events and frames marked as active recording.
- TCP frame rows include main-camera JPEG payload as base64 when a receiver is
  connected. The rolling buffer still stores only lightweight metadata and does
  not retain ARKit `CVPixelBuffer` references for seconds.

## Local Debug File

Each recording session creates one JSONL file:

```text
Documents/Recordings/capture_YYYYMMDD_HHMMSS_xxxxxxxx.jsonl
```

The first line is a `session_start` row. Each following line is one frame row
with:

- sequence
- aligned and local timestamps
- timebase status
- ARKit tracking state
- user-world camera 6DoF when available
- gripper opening and gripper sample age when available
- main camera RGB width/height/pixel format
- flags

The local debug file intentionally does not include RGB payload bytes. It is for
checking timing, pose, gripper pairing, and state transitions. The live TCP raw
stream can include JPEG RGB payload bytes for the Mac receiver.

To list local debug recordings from the Mac:

```sh
DEVELOPER_DIR=Xcode.app/Contents/Developer \
xcrun devicectl device info files \
  --device 00008130-000E2DA10141001C \
  --domain-type appDataContainer \
  --domain-identifier com.local.umift.capturecore \
  --subdirectory Documents/Recordings
```

To pull a recording:

```sh
DEVELOPER_DIR=Xcode.app/Contents/Developer \
xcrun devicectl device copy from \
  --device 00008130-000E2DA10141001C \
  --domain-type appDataContainer \
  --domain-identifier com.local.umift.capturecore \
  --source Documents/Recordings/<file>.jsonl \
  --destination ${UMIFT_ROOT}/modules/02_iphone_gripper/recordings/
```

## USB/TCP Stream

The first real-time transport is a newline-delimited JSON stream on TCP port
`17381`. The iPhone app keeps a normal TCP server API. The Mac receiver talks to
that port through usbmux when the phone is connected by cable.

Current stream behavior:

- server starts when the app starts
- `Recording V1 / transport` shows `listening 17381` or connected client count
- entering normal preview creates a `stream_*` session and sends a
  `session_start` row
- each preview/countdown/recording frame sends JSONL to connected
  Mac receivers
- recording start sends `recording_event: record_start`
- recording stop sends `recording_event: record_stop`
- frame rows include `recordingActive` and flags bit 4 when they belong to the
  valid recording segment
- the Mac-side receiver uses those events/flags to create the recording-only
  output while still preserving the full continuous stream
- TCP rows include JPEG RGB payload; local debug files omit payload bytes

Primary Mac receiver:

```sh
python3 ${UMIFT_ROOT}/modules/04_laptop_alignment_export/iphone_stream_receiver.py
```

Optional raw TCP test if an external port forwarder is used:

```sh
nc 127.0.0.1 17381
```

Offline receiver validation using a pulled local JSONL:

```sh
python3 ${UMIFT_ROOT}/modules/04_laptop_alignment_export/iphone_stream_receiver.py \
  --input-jsonl /path/to/capture.jsonl \
  --session-id offline_check
```

Default output path:

```text
${UMIFT_ROOT}/modules/04_laptop_alignment_export/captures/iphone_stream/<session_id>/
```

Current Mac note: Xcode `devicectl` can install, launch, and copy app files, but
this local Xcode does not expose a USB TCP port-forward command. The module 04
receiver talks to `/var/run/usbmuxd` directly, so `iproxy` is not required for
the normal receiver path.

## Important Boundary

The internal capture format and the transmitted format are intentionally
different.

Internal capture can be richer because it is used for alignment, debugging, and
future local buffering:

- wide ARKit frame timestamp and metadata
- wide pose and intrinsics
- depth summary, and later optional depth payload
- private ultrawide timestamp and metadata
- gripper opening, confidence, marker details, and calibration status
- sync age/error and events

The transmitted format must be fixed, narrow, and stable. For the first usable
version, it only contains four things:

- gripper opening
- camera 6DoF
- main camera RGB
- timestamp aligned across devices

The desktop side should not need to understand every internal debug field.

## Transmit Packet V1

The first transport packet is one aligned capture frame:

```text
RecordingTransmitPacketV1 {
  schema_version: UInt16 = 1
  sequence: UInt32
  aligned_timestamp_ns: Int64
  local_capture_timestamp_ns: Int64
  timebase_status: UInt8

  pose_x: Float
  pose_y: Float
  pose_z: Float
  roll_deg: Float
  pitch_deg: Float
  yaw_deg: Float

  gripper_open_percent: Float

  rgb_width: UInt16
  rgb_height: UInt16
  rgb_encoding: UInt8
  rgb_payload: bytes

  flags: UInt32
}
```

Timestamp rule:

- `aligned_timestamp_ns` is the timestamp that the desktop should use for
  cross-device alignment.
- `local_capture_timestamp_ns` is only a diagnostic fallback from the phone's
  local capture timeline.
- `timebase_status` tells whether the aligned timestamp is already synchronized
  or still a local estimate.

RGB rule:

- The payload is the main wide camera image at the same aligned timestamp.
- V1 allows `bgra8888` for low-latency raw frames and `jpeg` for smaller USB
  payloads.
- Transport can split the bytes into chunks, but the logical frame still uses
  this schema.
- The payload must be copied or encoded into app-owned `Data` before entering
  the long-lived transmit queue. Do not store ARKit-owned `CVPixelBuffer`
  references in the 3-second ring buffer.

V1 intentionally does not transmit:

- depth frame bytes
- ultrawide frame bytes
- gripper confidence
- gripper distance
- sync error
- center depth
- marker corners
- rejected candidates
- full camera intrinsics
- event log history

If those become necessary, they should be added as separate packet types or a
new schema version, not by growing V1 into a debug dump.

## Flags

```text
bit 0 pose_valid
bit 1 gripper_valid
bit 2 rgb_valid
bit 3 timebase_synchronized
bit 4 recording
```

Unavailable float fields should use `NaN`; validity is determined by flags.

## Next Implementation Steps

1. Verify live JSONL frames arrive through the module 04 receiver while the app
   is calibrated but not recording, then verify `record_start` marks the valid
   segment.
2. Add desktop-to-phone timebase messages over the same connection and move
   `timebase_status` from `local only` to `estimating/synchronized`.
3. Replace JPEG base64 JSON rows with compact chunked binary RGB once the field
   set is stable.
4. Keep transport separate from capture, so USB/network/file can share the same
   packet schema.
5. Add desktop receiver acknowledgements and dropped-frame counters.
