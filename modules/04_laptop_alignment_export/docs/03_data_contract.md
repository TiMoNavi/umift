# 模块 04 数据契约 V2

当前多路时间戳、H.264 主帧和插值规则以本文为准。

## 1. 分层原则

```text
raw/
  忠实保存设备输出，不做训练格式假设。

normalized/
  把各设备转换成统一 JSONL 行。只统一字段和时间戳，不重采样。

aligned/
  根据 episode 边界和最近邻/插值规则生成统一 timeline。

exports/
  从 aligned timeline 生成具体训练或调试格式。
```

所有跨设备对齐都使用：

```text
aligned_monotonic_ns  # Mac time.monotonic_ns()
```

录制控制约定：

```text
Mac GUI button = Arm / Preview only
iPhone record_start = open disk recording gates for all streams
iPhone record_stop  = close disk recording gates for all streams
```

待命/预览期间，设备可以持续发送数据给 GUI，但不写入正式训练 raw artifacts。

每一路 live stream 都有同一套录制缓冲语义：

```text
preview/live data -> timestamped ring buffer in memory
record_start accepted -> flush [event_time - pre_roll_s, now] into episode writers
recording active      -> append live rows/frames directly
record_stop           -> close episode writers
```

默认值：

```text
pre_roll_s      = 1.5
stream_buffer_s = 3.0
```

pre-roll 是 raw 层的安全缓冲，不改变官方 episode 边界；`aligned/episodes/episode_XXXXXX/episode.json`
仍以对应 iPhone `record_start` / `record_stop` 为准。缓冲出来的额外 raw 行保留真实时间戳，Episode 级 normalize 会按窗口裁剪。

录制校验门：

```text
record_start arrives
  -> live preflight checks iPhone + D435 + Teensy
  -> pass: open disk writers
  -> fail: reject record_start, keep preview/live only, show reasons in GUI
```

当前 live preflight 条件：

- iPhone receiver 正在运行，已有 frame，最近 frame 不超过 `2.0 s`。
- iPhone time alignment 状态是 `estimating` 或 `receiver_aligned`。
- D435 preview/recording 进程在运行，`latest_rgb.jpg` 和 `latest_depth.png` 不超过 `2.5 s`。
- Teensy/CoinFT live stream 在运行，已有 sample，最近 sample 不超过 `1.0 s`。
- iPhone 与 Teensy 最近 host monotonic 时间差不超过 `1.5 s`。

如果 preflight 不通过，手机端可以继续自己的录制 UI，但 Mac 不写入 run/raw 训练数据。

## 2. Run Layout

```text
runs/<run_id>/
├── RUN_MANIFEST.json
├── logs/SYNC_LOG.jsonl
├── raw/
│   ├── iphone_stream/<session_id>/
│   │   ├── raw_iphone_stream.jsonl
│   │   ├── aligned_iphone_stream.jsonl
│   │   ├── recording_only_iphone_stream.jsonl
│   │   └── iphone_rgb_frames/
│   ├── global_camera/
│   │   └── episode_000000/
│   │       ├── rgb.mp4
│   │       ├── depth.raw
│   │       ├── frame_timestamps.csv
│   │       └── camera_metadata.json
│   └── coinft/
│       └── episode_000000/
│           └── raw_coinft_stream.csv 或 teensy_mock_stream.csv
├── normalized/
│   ├── iphone_frames.jsonl
│   ├── recording_events.jsonl
│   ├── d435_frames.jsonl
│   ├── coinft_samples.jsonl
│   └── NORMALIZE_REPORT.json
├── aligned/
│   ├── timeline.jsonl
│   ├── episodes.jsonl
│   └── ALIGNMENT_REPORT.json
└── exports/
```

## 3. Raw Sources

### iPhone CaptureCore

来源：TCP/usbmux JSONL port `17381`。

原始行仍完整写入：

```text
raw/iphone_stream/<session_id>/raw_iphone_stream.jsonl
```

当前 app 会发送：

- `rowType=frame`
- `rowType=recording_event`

录制事件：

- `eventCode=record_start`
- `eventCode=record_stop`

iPhone receiver 还会生成 receiver-side normalized 文件：

```text
raw/iphone_stream/<session_id>/aligned_iphone_stream.jsonl
raw/iphone_stream/<session_id>/recording_only_iphone_stream.jsonl
```

其中 `aligned_iphone_stream.jsonl` 不复制 JPEG base64 payload；payload 保留在 raw JSONL 和
`iphone_rgb_frames/`。

GUI 采集模式下，`raw_iphone_stream.jsonl` / `aligned_iphone_stream.jsonl` 只持久化通过 Mac
录制窗口的事件和 frame。非录制预览帧只用于 GUI 显示、时间估计和短时 pre-roll buffer。
未通过录制校验门时，iPhone 预览通过 fMP4 片段写入当前 session 的 `preview/` 目录，由 HTTP handler 的 `/iphone/preview/` 路由提供给浏览器。
Mac 持久化窗口内的 frame 会写：

```json
{
  "recording": {
    "active": true,
    "source_active": false,
    "mac_window_active": true,
    "persisted_by": "mac_recording_window 或 iphone_preroll_buffer",
    "preroll": false
  }
}
```

其中 `source_active` 是手机原始 frame flag，`active` 是 Mac 接收端实际用于落盘和后续训练索引的窗口。

### D435

正式落盘必须走固定入口：

```bash
sudo -n /usr/local/libexec/umift-global-camera/control_capture_macos.sh start ...
```

输出：

```text
raw/global_camera/episode_xxxxxx/rgb.mp4
raw/global_camera/episode_xxxxxx/depth.raw
raw/global_camera/episode_xxxxxx/frame_timestamps.csv
raw/global_camera/episode_xxxxxx/camera_metadata.json
```

D435 对齐锚点：

```text
frame_timestamps.csv.host_receive_time_s
```

D435 在 sudo/root 相机进程内维护 RGB/depth ring buffer。控制文件里的
`event_aligned_unix_ns` 和 `recording_preroll_s` 用来切出 pre-roll；`frame_timestamps.csv`
保留每帧原始 `host_receive_time_s`，normalize 阶段再用 iPhone timebase 转成 `aligned_monotonic_ns`。

### CoinFT / Teensy

真实采集脚本：

```text
modules/03_coinft_teensy/scripts/collect_coinft_raw.py
```

当前 GUI 也可写 mock CSV：

```text
raw/coinft/episode_xxxxxx/teensy_mock_stream.csv
```

CoinFT 原始接收时间：

```text
host_receive_monotonic_ns
```

当前 normalize 已拟合：

```text
mac_monotonic_ns = a * teensy_time_us + b
```

## 4. Normalized Rows

### `normalized/iphone_frames.jsonl`

关键字段：

```json
{
  "row_type": "normalized_iphone_frame",
  "run_id": "...",
  "source_stream": "iphone",
  "session_id": "...",
  "sequence": 0,
  "aligned_monotonic_ns": 123,
  "recording": {"active": true},
  "pose": {"valid": true, "x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
  "gripper": {"valid": true, "open_percent": 50.0},
  "rgb": {"image_path": "raw/iphone_stream/.../frame_00000000.jpg"}
}
```

### `normalized/recording_events.jsonl`

```json
{
  "row_type": "normalized_recording_event",
  "aligned_monotonic_ns": 123,
  "event": {"code": "record_start", "index": 0}
}
```

### `normalized/episodes/episode_XXXXXX/d435_frames.jsonl`

```json
{
  "row_type": "normalized_d435_frame",
  "frame_index": 0,
  "aligned_monotonic_ns": 123,
  "rgb": {"video_path": "raw/global_camera/rgb.mp4", "frame_index": 0},
  "depth": {"raw_path": "raw/global_camera/depth.raw", "byte_offset": 0}
}
```

### `normalized/episodes/episode_XXXXXX/coinft_samples.jsonl`

```json
{
  "row_type": "normalized_coinft_sample",
  "packet_index": 0,
  "sequence_id": 0,
  "aligned_monotonic_ns": 123,
  "left_raw": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  "right_raw": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
}
```

## 5. Aligned Timeline

`aligned/episodes/episode_XXXXXX/episode.json` 来自该 source Episode 匹配到的唯一 iPhone `record_start` / `record_stop`。自动链路不再退回整段 session 或全范围。

`aligned/episodes/episode_XXXXXX/timeline.jsonl` 每行是一个训练候选样本索引：

```json
{
  "row_type": "aligned_sample",
  "episode_index": 0,
  "sample_index": 0,
  "aligned_monotonic_ns": 123,
  "target_stream": "iphone",
  "iphone": {"valid": true, "source_delta_ms": 0.0},
  "d435": {"valid": true, "source_delta_ms": 4.2, "frame_index": 10},
  "coinft": {"valid": true, "source_delta_ms": -1.1, "packet_index": 100},
  "valid_mask": {"iphone": true, "d435": true, "coinft": true}
}
```

默认阈值：

- D435 nearest frame：`80 ms`
- CoinFT nearest sample：`25 ms`

阈值外不会删除样本，但会标：

```json
{"valid": false, "dropped_reason": "nearest_sample_outside_threshold"}
```

## 6. Export

导出入口：

```bash
python3 modules/04_laptop_alignment_export/entrypoints/export_run.py runs/<run_id> <source_episode_index> \
  --format umift-replay-buffer-zarr
```

默认 registry 当前只开放 `umift-replay-buffer-zarr`：按 `/Users/550m/code/UMI-FT` 的 episode replay buffer 结构增量写 Zarr。历史调试/ForceFlow exporter 不再接入自动链路、GUI 或默认 CLI。

`umift-replay-buffer-zarr` 的核心字段：

- `data/episode_i/rgb_0`
- `data/episode_i/ts_pose_fb_0`
- `data/episode_i/gripper_0`
- `data/episode_i/wrench_left_0`
- `data/episode_i/wrench_right_0`
- `data/episode_i/wrench_concat_0`
- `data/episode_i/*_time_stamps_0`
- `meta/episode_*_len`

注意：

- `ts_pose_fb_0` 由 exporter 将 iPhone/user_world pose 转成原 UMI-FT GoPro/tool 口径，格式为 `[x,y,z,qw,qx,qy,qz]`。
- 当前不要求采集 robot base 下的绝对 EEF pose。
- `ts_pose_command_0` 第一版可以等于 `ts_pose_fb_0`。
- `ts_pose_virtual_target_0` 和 `stiffness_0` 是后处理/占位字段，不是采集层直接返回的数据。

详细要求见 `docs/04_export_contract.md`。
