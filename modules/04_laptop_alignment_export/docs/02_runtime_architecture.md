# 主链路、状态和录制事件流

> 当前运行契约以本文为准。更早的三路采集设计细节已从交付包中移除。

本文档描述当前 `modules/04_laptop_alignment_export` 的主流程。这里的“主链路”指笔记本端唯一负责设备编排、GUI 命令、录制状态、门控检查和状态广播的控制层。

当前实现位置：

```text
entrypoints/receiver_web_gui.py
  -> umift_laptop_alignment/app/receiver_web_gui.py
```

Normalize 和 Align 已迁入 `umift_laptop_alignment/pipeline/normalize/` 与 `pipeline/alignment/`。最终训练格式只通过 `pipeline/export/` 下的独立 exporter plugin 生成。

## 1. 总原则

采集和录制是两件事。

```text
主程序启动
  -> 三路设备持续采集
  -> 三路各自进入 preview / buffer
  -> iPhone record_start 只声明“从这个时间点开始的数据有效”
  -> 主链路用同一个 canonical event time 通知各录制窗口
```

因此：

- iPhone / D435 / CoinFT 都不是按下录制键后才开始采集。
- 录制键只决定哪些时间窗口需要落盘进入 episode。
- 采集层只保存 raw，不做训练格式 action、pose delta、gripper 二值化或 force 映射。
- GUI 不直接控制设备内部实现，只调用主链路命令并读取主链路状态。

## 2. GUI 边界

GUI 是主链路的预览和图形化操作窗口。

GUI 允许：

- 读 `/state`。
- 显示 `capture_summary`、iPhone 状态、D435 预览、CoinFT 曲线。
- 向 `/main/command` 发送按钮命令。

GUI 不允许：

- 直接 import 或调用三路 receiver。
- 直接读写 D435 sudo 控制文件。
- 直接决定 recording gate。
- 直接调用落盘、对齐、转换、Zarr 模块形成业务流程。

按钮事件统一是：

```text
GUI button
  -> POST /main/command { command, args }
  -> ReceiverBackend.handle_main_command()
```

当前主命令包括：

```text
ensure_run
arm_capture
arm_all
stop_capture
start_coinft
stop_coinft
start_d435_preview
stop_d435
normalize
align
export_umift_zarr
open_output
```

## 3. 启动流程

常用启动命令：

```bash
python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8899 \
  --no-open \
  --coinft-port /dev/cu.usbmodemXXXXXX \
  --coinft-simulate
```

默认 `--auto-start` 打开，因此 GUI server 启动后会自动执行主链路的采集启动逻辑。

实际流程：

```text
main()
  -> ReceiverBackend(...)
  -> ThreadingHTTPServer(...)
  -> auto-start enabled
  -> backend.arm_capture()
       -> ensure_active_run()
       -> start_receiver()       # iPhone
       -> start_coinft()         # Teensy / CoinFT
       -> start_d435_preview()   # D435 continuous stream
       -> refresh_preflight_state()
       -> capture_summary
```

`ensure_active_run()` 创建或复用 run 目录：

```text
runs/<run_id>/
  RUN_MANIFEST.json
  logs/SYNC_LOG.jsonl
  raw/
  normalized/
  aligned/
  exports/
```

## 4. 主状态结构

主链路拥有唯一状态字典 `ReceiverBackend.state`。GUI 通过 `/state` 读取它。

最重要的稳定字段是：

```text
capture_summary
recording_control
recording_pipeline
recording_gate
capture_options
buffering
stats
alignment
d435
d435_recording
teensy
latest_frame
latest_iphone_pose
```

### `capture_summary`

`capture_summary` 是 GUI 和人工判断应优先看的汇总状态。它由 `build_capture_summary_locked()` 从底层状态汇总而来。

结构：

```text
capture_summary:
  status: idle | ready | blocked | recording
  ready: bool
  updated_unix_ns
  gate_reasons: list[str]
  recording:
    armed
    active
    episode_index
    blocked
    blocked_reason
    started_at
    started_at_unix_ns
    last_event
  pipeline:
    phase
    transition_seq
    current_episode_index
    canonical_event_time
    last_transition
    post_start_check
  streams:
    iphone
    d435
    coinft
```

三路 stream 汇总字段：

```text
iphone:
  ok
  running
  status
  frame_count
  age_ms
  buffer_samples
  buffer_span_ms
  pose
  last_error

d435:
  ok
  running
  recording
  pid
  age_ms
  depth_age_ms
  fps
  buffer_frames
  buffer_span_ms
  last_error

coinft:
  ok
  running
  source
  serial_port
  fps
  live_sample_count
  recorded_sample_count
  age_ms
  buffer_samples
  buffer_span_ms
  dropped_sequence_count
  timeout_count
  last_error
```

本次 smoke 中 `/state` 已验证：

```text
capture_summary.ready = true
streams.iphone.ok = true
streams.d435.ok = true
streams.coinft.ok = true
recording_control.armed = true
recording_control.active = false
recording_pipeline.phase = idle
```

### `recording_control`

`recording_control` 是当前录制窗口的即时控制状态：

```text
recording_control:
  armed: bool
  active: bool
  episode_index: int | null
  started_at: aligned_monotonic_ns | null
  started_at_unix_ns: aligned_unix_ns | null
  stopped_at: aligned_monotonic_ns | null
  stopped_at_unix_ns: aligned_unix_ns | null
  last_event: str
  blocked: bool
  blocked_reason: str | null
  source: iphone_recording_event | post_start_gate_check | ...
```

`active=true` 表示三路数据在当前 episode 中有效，写盘窗口应打开。

### `recording_pipeline`

`recording_pipeline` 是给落盘、对齐、变换、Zarr 等下游模块观察的事件流状态：

```text
recording_pipeline:
  phase: idle | starting | active | stopping | stopped | error
  transition_seq: int
  current_episode_index: int | null
  canonical_event_time:
    event_aligned_monotonic_ns
    event_aligned_unix_ns
    source
  last_transition: dict | null
  transitions: list[dict]
  post_start_check:
    status: idle | scheduled | ok | failed
    delay_s
    episode_index
    checked_at_unix_ns
    reasons
```

下游模块不互相引用，也不反向 import 主链路。它们只需要保存自己处理到的 `transition_seq`，发现序号增加后读取 `last_transition` 或 bounded `transitions`。

## 5. Canonical Event Time

iPhone `record_start` / `record_stop` 到达主链路时，主链路只采用一个 canonical event time：

```text
timestamp.aligned_monotonic_ns
timestamp.aligned_unix_ns
```

它优先来自 iPhone receiver 的四时间戳双向 affine 映射；只有旧会话或同步样本缺失时才回退到 `one_way_min_delay`。

这个时间会写入：

```text
recording_control.started_at / stopped_at
recording_control.started_at_unix_ns / stopped_at_unix_ns
recording_pipeline.canonical_event_time
recording_pipeline.last_transition
D435 record-control file
CoinFT recording_window_snapshot()
iPhone recording-only writer decision
```

三路后续都应该围绕这个 canonical event time 决定：

- pre-roll 起点。
- 哪些数据属于当前 episode。
- post-start gate 失败时终止哪个 episode。

## 6. RecordingTransition

状态转换统一由 `publish_recording_transition()` 生成 `RecordingTransition`：

```text
orchestration/recording_events.py::RecordingTransition
```

字段：

```text
event
episode_index
created_unix_ns
event_aligned_monotonic_ns
event_aligned_unix_ns
run_dir
source
reason
gate
metadata
seq
```

事件会同时：

- 写入 `recording_pipeline.last_transition`。
- append 到 `recording_pipeline.transitions`，最多保留最近 40 条。
- 更新 `transition_seq`。
- 通过 SSE 广播 `recording_transition`。

phase 映射：

```text
record_start_requested           -> starting
record_start_committed           -> active
record_start_postcheck_ok        -> active
record_stop_requested            -> stopping
record_stop_committed            -> stopped
record_start_rejected            -> error
record_start_failed              -> error
record_start_postcheck_failed    -> error
```

## 7. record_start 流程

iPhone app 按下录制开始后，receiver 收到 `record_start` event，进入：

```text
ReceiverBackend.handle_iphone_recording_event()
```

流程：

```text
record_start
  -> 计算 episode_index
  -> 提取 aligned_monotonic_ns / aligned_unix_ns
  -> check_recording_gate()
  -> 如果 gate 失败：
       recording_control.active = false
       recording_control.last_event = record_start_rejected
       recording_control.blocked = true
       publish record_start_rejected
       不打开 D435 writer
  -> 如果 gate 通过：
       publish record_start_requested
       recording_control.active = true
       recording_control.last_event = record_start
       start_d435_recording(...)
       如果 D435 没有确认：
         active = false
         publish record_start_failed
       如果 D435 确认：
         publish record_start_committed
         schedule_post_start_gate_check()
```

D435 通过 `/tmp/umift_d435_record_control.json` 接收 start 请求，里面包含：

```text
command = start
episode_index
output_dir
run_id
duration_s
event_aligned_monotonic_ns
event_aligned_unix_ns
recording_preroll_s
```

CoinFT 不通过文件控制，而是在 serial worker 内轮询 `recording_window_snapshot()`。当 `active=true` 且 episode 变化时，它打开：

```text
run/raw/coinft/episode_xxxxxx/
  raw_coinft_packets.bin
  raw_coinft_stream.csv
```

iPhone recording-only writer 同样根据 recording window 把有效帧写入：

```text
run/raw/iphone_stream/<session_id>/recording_only_iphone_stream.jsonl
```

## 8. record_stop 流程

iPhone app 按下录制停止后：

```text
record_stop
  -> 如果 active=false:
       last_event = record_stop_ignored
       publish record_stop_ignored
  -> 如果 active=true:
       publish record_stop_requested
       recording_control.active = false
       last_event = record_stop
       stop_d435_recording()
       refresh_preflight_state()
       publish record_stop_committed
```

CoinFT 和 iPhone 因为读取的是 `recording_window_snapshot()`，会在 `active=false` 后自然关闭当前 episode writer。

## 9. Gate 检查

`check_recording_gate()` 是录制开始前和录制开始后复检共用的门控逻辑。

检查项：

```text
Mac capture is armed
iPhone receiver running
iPhone frame_count > 0
iPhone latest sample age <= 2.0s
iPhone time alignment status in {receiver_aligned, estimating}
iPhone pose world_calibrated = true
D435 preview/recording process running
D435 latest_rgb.jpg and latest_depth.png exist
D435 RGB/depth preview age <= 2.5s
D435 stream status has no lastError
Teensy/CoinFT running
Teensy/CoinFT live_sample_count > 0
Teensy/CoinFT latest sample age <= 1.0s
iPhone vs Teensy live monotonic skew <= 1.5s
```

失败原因进入：

```text
recording_gate.reasons
capture_summary.gate_reasons
recording_control.blocked_reason
RecordingTransition.gate
```

特别注意 iPhone 未标记/未标定状态：

```text
iPhone frame_count > 0
rgb.valid = true
gripper.valid = true
pose.valid = false
pose.world_calibrated = false
pose.world_origin_status = not_marked
```

这种状态说明采集链路是活的，但训练 pose/action 不能用。主链路必须保持：

```text
capture_summary.ready = false
recording_gate.status = blocked
gate_reasons includes "iPhone world origin is not calibrated (not_marked)"
```

raw 数据仍可保留用于调试；正式 episode 和 Zarr mapping 不能把这些帧当作有效 EEF pose。

## 10. Post-Start Gate Check

`record_start_committed` 之后，主链路会延迟复检：

```text
POST_START_GATE_CHECK_DELAY_S = 2.5
```

如果复检通过：

```text
post_start_check.status = ok
publish record_start_postcheck_ok
phase = active
```

如果复检失败：

```text
recording_control.active = false
recording_control.last_event = record_start_postcheck_failed
recording_control.blocked = true
recording_pipeline.phase = error
post_start_check.status = failed
publish record_start_postcheck_failed
stop_d435_recording()
```

这样可以处理“刚开始时看起来通过，但几秒后某一路掉线/卡住”的情况。

## 11. Buffer 设置

默认设置：

```text
DEFAULT_RECORDING_PREROLL_S = 0.75
DEFAULT_STREAM_BUFFER_S = 3.0
IPHONE_BUFFER_MAX_FRAMES = 240
CoinFT DEFAULT_MAX_SAMPLES = 6000
CoinFT MAX_ITEMS_PER_BUFFER_SECOND = 2000
D435 stream buffer defaults to 3.0s
```

`arm_capture()` 会保证：

```text
stream_buffer_s >= recording_preroll_s + 0.5
```

统一 ring buffer 行为在：

```text
capture/buffers/timestamped_ring.py
```

规则：

- append 时必须有 `timestamp_ns`。
- buffer 按 `max_age_s` 和 `max_items` 修剪。
- `slice_window(start_ns, end_ns)` 用于 pre-roll flush。
- `preroll_start_ns(event_timestamp_ns, preroll_s)` 计算事件前窗口起点。

三路 buffer 作用：

```text
iPhone:
  IPhoneRawBuffer
  max_age_s = stream_buffer_s
  max_items = 240

D435:
  D435FrameRingBuffer inside capture_d435i.py
  stream_buffer_s from main chain
  status reports buffer.frames and buffer.spanMs

CoinFT:
  CoinFTRawBuffer
  max_age_s = stream_buffer_s
  max_items = max(6000, stream_buffer_s * 2000)
```

## 12. 本次运行验证

本轮验证执行了：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q entrypoints umift_laptop_alignment tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q -p no:cacheprovider
python3 entrypoints/receiver_web_gui.py --help
python3 entrypoints/iphone_stream_receiver.py --help
bash entrypoints/install_sudoers_macos.sh --help
python3 entrypoints/export_run.py --help
```

还启动了一次 live GUI smoke：

```bash
python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8899 \
  --no-open \
  --coinft-port /dev/cu.usbmodemXXXXXX \
  --coinft-simulate
```

`/state` 结果：

```text
capture_summary.ready = true
iphone.ok = true
d435.ok = true
coinft.ok = true
recording_control.armed = true
recording_control.active = false
recording_pipeline.phase = idle
```
