# 模块 04：笔记本端三路采集与 UMI-FT 导出

这个模块现在负责笔记本侧主链路：启动并观察三路原始采集，维护统一运行状态，给 GUI 提供预览和操作入口，并通过 exporter plugin 导出 UMI-FT replay buffer Zarr。

当前重构原则很简单：采集层只采集设备原样返回的数据，不在这里做训练格式语义转换。录制按钮只声明某段时间的数据有效，三路采集本身在主程序启动后就持续运行并进入各自缓冲区。

## 当前目录结构

整个 `modules/04_laptop_alignment_export/` 现在按“入口、实现包、配置、文档、测试”分开：

```text
modules/04_laptop_alignment_export/
├── README.md
├── entrypoints/
├── umift_laptop_alignment/
├── config/
├── docs/
├── tests/
└── captures/                  # local ignored data, if historical captures exist
```

当前仍建议直接运行的入口统一放在 `entrypoints/`：

- `entrypoints/receiver_web_gui.py`：主 GUI / 三路采集入口。
- `entrypoints/iphone_stream_receiver.py`：iPhone Protocol V2 H.264 archive receiver 单独调试入口。
- `entrypoints/normalize_run.py`、`entrypoints/align_run.py`、`entrypoints/export_run.py`：指定单个 Episode 的离线增量处理入口。
- `entrypoints/install_sudoers_macos.sh`：D435 一次性授权入口。

旧 Tk GUI、旧同步框架和无调用方的兼容 wrapper 已从当前交付主线移除，并保留在交付前回滚备份中。Normalize/Align 已迁入正式 pipeline；历史 ForceFlow exporter 仅保留在 `pipeline/export/forceflow/` 供兼容测试使用。

`captures/` 是历史采集输出目录，已经被 `.gitignore` 忽略。它不是源码结构的一部分；如果要让根目录视觉上彻底没有数据目录，需要单独确认后再迁移这批本地数据。

`umift_laptop_alignment/` 包内现在只保留三块顶层职责：

```text
umift_laptop_alignment/
├── capture/
│   ├── buffers/
│   │   └── timestamped_ring.py
│   └── receivers/
│       ├── iphone/
│       ├── d435/
│       └── coinft/
├── orchestration/
│   ├── recording_events.py
│   └── run_layout.py
└── pipeline/
    ├── disk/
    ├── alignment/
    ├── transforms/
    └── export/
```

命名规则：

- `capture/`：只放原始采集、设备协议、接收时间戳和采集缓冲区。
- `capture/receivers/<device>/`：每一路设备一个独立目录，当前有 `iphone`、`d435`、`coinft`。
- `capture/buffers/`：跨设备可复用的时间戳 ring buffer。
- `orchestration/`：主链路共享状态、录制事件、run 目录布局，不放设备协议细节。
- `pipeline/`：录制后的处理层，包括落盘、对齐、三路标准化变换、UMI-FT 字段映射和 exporter plugin。
- `entrypoints/` 内的 `receiver_web_gui.py`、`iphone_stream_receiver.py`、`normalize_run.py`、`align_run.py`、`export_run.py` 是薄入口 wrapper。

`umift_laptop_alignment/app/receiver_web_gui.py` 目前仍承载 GUI 后端的大部分实现，并调用新的 `capture/`、`orchestration/`、`pipeline/` 包结构。Normalize/Align 复用现有解析器，但入口已经严格限定为单个 Episode；最终导出通过 `pipeline/export/controller.py` 调用 Episode 级增量 exporter。GUI 和默认 registry 不再暴露 run 级全量导出。

详细流程文档：

- `docs/01_operator_guide.md`：面向首次使用者的完整指南，包括首次部署命令、项目和数据目录层级、启动服务、创建/恢复 Dataset、Arm All、iPhone 校准、开始/结束录制和切换任务；电脑端 B-01 至 B-06 和 iPhone 原生 I-01 至 I-05 截图已经嵌入。
- `../../docs/iphone_mac_realtime_stream_rewrite.md`：iPhone 1920x1440@30 H.264、二进制协议、可靠 spool/ACK 和 Mac receiver 模块拆分目标。
- `docs/02_runtime_architecture.md`：主链路状态、事件、canonical event time、record_start/stop、post-start gate。
- `docs/03_data_contract.md`：三路原始数据、时间戳、Episode 和对齐字段契约。
- `docs/04_export_contract.md`：当前主线要求。说明如何把 iPhone SLAM 轨迹、图像、夹爪、双路 CoinFT 和 D435 全局相机导出为 UMI-FT replay buffer Zarr；文末也写明 iPhone app / Mac receiver 后续要改造成什么目标契约。
- `docs/05_troubleshooting.md`：设备、GUI、时间戳、后处理和导出故障排查。
- `docs/archive/`：历史设计、旧链路说明和原始截图，仅供追溯，不作为当前运行契约。

UMI-FT/Zarr 规则速查：

- 不再要求采集机器人绝对 EEF pose；`ts_pose_fb_0` 由 exporter 把 iPhone/user_world pose 转成原 UMI-FT GoPro/tool 口径。
- `ts_pose_fb_0` 使用 pose7：`[x, y, z, qw, qx, qy, qz]`，不是 Euler。
- `rgb_0` 使用 UMI-FT 的 THWC 图像：`(T, 224, 224, 3)`。
- D435 全局相机 V0 写成 `rgb_global_0` 扩展字段，同时写 `depth_0` 标准字段。
- 双路 CoinFT 原坐标写入 `_coinft_0` 字段，`wrench_left_0`、`wrench_right_0`、`wrench_concat_0` 在 exporter 端套用原 UMI-FT CoinFT->TCP/tool 外参。
- 每路保留自己的 timestamp array，UMI-FT dataset/sampler 通过 timestamps 和 relative pose 逻辑对齐。
- 第一版可设置 `ts_pose_command_0 = ts_pose_fb_0`；`ts_pose_virtual_target_0` 和 `stiffness_0` 由后处理生成或显式占位。
- 最终导出必须走 exporter plugin/registry；GUI 和主链路不能硬编码某一种 Zarr schema。

## 三路采集现状

### iPhone

位置：`umift_laptop_alignment/capture/receivers/iphone/`

职责：

- 通过 usbmux 或 TCP 接收 iPhone 端 CaptureCore 数据。
- 保存 iPhone 返回的原始 frame/event 字段。
- 将最近样本放入 `IPhoneRawBuffer`，供主链路汇总和录制门控读取。
- iPhone 录制按钮仍是 episode 的主要控制源：`record_start` / `record_stop` 进入主链路状态。

### D435

位置：`umift_laptop_alignment/capture/receivers/d435/`

职责：

- 从 `modules/01_global_camera` 迁入 D435 采集链路。
- 采集需要 root 权限，因此通过固定 sudo 入口控制：
  `/usr/local/libexec/umift-laptop-alignment-d435/control_capture_macos.sh`
- 主程序启动后可以启动 D435 preview/stream，数据先进入独立缓冲区和 preview 文件。
- 录制状态有效后，再由主链路下发控制状态，决定哪些时间窗口需要落盘。

一次性授权命令：

```bash
python3 entrypoints/receiver_web_gui.py --install-d435-sudoers
```

### CoinFT / Teensy

位置：`umift_laptop_alignment/capture/receivers/coinft/`

职责：

- 通过 USB serial 接收 Teensy 转发的 CoinFT-like 二进制帧。
- 当前 Teensy 端口是 `/dev/cu.usbmodem183837501`。
- 已支持 `m` 模拟模式，便于没有真实 CoinFT 时验证链路。
- 样本进入 `CoinFTRawBuffer`，主链路只看新鲜度、数量、最近时间戳等汇总状态。

## 主链路和 GUI 边界

GUI 只是预览和图形化操作窗口，不直接操作设备内部实现。

当前边界：

- GUI 读 `/state`，展示主链路维护的 `capture_summary`、录制状态、最近错误和各路新鲜度。
- GUI 按钮调用 `/main/command`。
- 按钮对应的实际行为写在主链路里，例如 `arm_all`、`stop_capture`、`start_coinft`、`start_d435_preview`。
- 主链路负责启动三路采集、维护缓冲区引用、汇总实时状态、处理录制事件和门控错误。

也就是说，GUI 不知道 D435 sudo 脚本、Teensy 串口 worker、iPhone receiver 的内部细节；它只触发主链路命令并读取主链路状态。

主链路现在还维护一个历史 live rows/Zarr 转换状态。这里的内部字段名暂时沿用旧命名，最终导出目标已改为 UMI-FT replay buffer Zarr：

- `/state.state.zarr_conversion`：完整 live 转换状态。
- `/state.state.zarr_recording`：录制窗口内 converted rows 的落盘状态。
- `/state.state.capture_summary.zarr_conversion`：给 GUI/状态面板用的简短汇总。
- `/state.state.capture_summary.zarr_recording`：给 GUI/状态面板用的落盘汇总。
- 当前写的是 `pipeline/live_zarr_rows/episode_xxxxxx/completed_rows.jsonl` 和 `MANIFEST.json`，不是最终 UMI-FT `.zarr`。
- 转换在三路 gate ready 后开始；录制键打开/关闭 converted-row writer。最终 UMI-FT Zarr writer 后续读取这些 rows 再写 episode arrays。
- pose 语义默认是 `iphone_user_world_slam`：SLAM camera/gripper trajectory state，不是机器人绝对 EEF pose。

最终导出选择已经独立成 `pipeline/export/controller.py`：

- `/state.state.export_control`：主链路维护的导出选择状态，包含 `selected_format`、`selected_profile`、`available_formats`、`available_profiles`、`status`、`last_manifest`、`last_error`。
- GUI 的 Export format / Export profile 控件只更新 `export_control`，不直接拼 Zarr 字段。
- `/main/command` 的 `configure_export` 只改变导出选择；`export_selected` 对指定或最新完成的 Episode 做增量提交。
- 默认 registry 当前只开放 `umift-replay-buffer-zarr` + `config/export_profiles/umift_replay_buffer_v0.json`。历史 ForceFlow exporter 仅保存在 `pipeline/export/forceflow/` 供兼容测试使用，主 GUI、自动链路和默认 CLI 不会注册或调用它。

## 录制事件流

采集和录制是两件事：

```text
主程序启动
  -> 三路采集持续运行
  -> 三路各自进入缓冲区和预览状态
  -> iPhone 发 record_start
  -> 主链路生成 canonical event time
  -> 主链路更新 recording_control / recording_pipeline 状态
  -> 落盘、对齐、变换/Zarr 模块监听状态
```

`canonical event time` 会记录在 `orchestration/recording_events.py::RecordingTransition` 里，核心字段包括：

- `event`
- `episode_index`
- `created_unix_ns`
- `event_aligned_monotonic_ns`
- `event_aligned_unix_ns`
- `run_dir`
- `gate`

录制开始后，主链路会在短延迟后做一次 post-start gate check。当前延迟是 `2.5s`。如果三路新鲜度、缓冲区或时间状态不对，门控可以向主链路返回错误停止信息，由主链路统一更新状态。

## Pipeline 与导出插件

后处理代码按职责分层，最终训练格式由 `pipeline/export/` 下的 exporter plugin 负责：

```text
pipeline/
├── disk/
│   └── live_zarr_recorder.py
├── alignment/
│   └── runner.py
├── normalize/
│   └── runner.py
├── transforms/
│   ├── iphone_v2.py
│   └── live_zarr.py
├── postprocess/
│   └── orchestrator.py
└── export/
    ├── base.py
    ├── controller.py
    ├── registry.py
    ├── cli.py
    ├── umift_replay_buffer/
    │   └── exporter.py
    └── forceflow/              # 历史兼容代码
```

设计意图：

- `disk/`：录制窗口内 live rows 的实际落盘实现。
- `alignment/`：把三路 raw 数据对齐到统一时间轴。
- `normalize/`：把三路原始文件标准化为 Episode 级中间数据。
- `transforms/`：iPhone V2 归一化和录制期 live-row 状态；不冒充最终训练 Zarr。
- `export/forceflow/`：历史兼容代码，不再是当前主线。
- `postprocess/`：按 source Episode 调用 normalize、align、质量门控和增量 export；不同 Episode 可后台并行，Zarr 提交阶段按 Dataset 加锁。
- `export/umift_replay_buffer/`：当前主线，只构建并原子提交一个 Episode，不删除或重写已有 Zarr Episode。
- 最终训练 Zarr 只由 `export/umift_replay_buffer/` 写入。

训练格式相关理解放在 exporter/mapping 层，不反向改采集层。例如：夹爪采集到的是连续开合值，就在 raw/standard 中保留连续值；UMI-FT exporter 再写入 `gripper_0`。

当前 UMI-FT 单 Episode 增量导出命令：

```bash
python3 entrypoints/export_run.py <run_dir> <source_episode_index> \
  --format umift-replay-buffer-zarr \
  --config config/export_profiles/umift_replay_buffer_v0.json
```

输出目录：

```text
<run_dir>/exports/umift_replay_buffer_zarr/
  EXPORT_CONFIG.json
  UMIFT_EXPORT_MANIFEST.json
  acp_replay_buffer_gripper.zarr/
```

中间文件按 source Episode 隔离：

```text
<run_dir>/normalized/episodes/episode_000012/
<run_dir>/aligned/episodes/episode_000012/
```

Zarr 内部使用连续逻辑编号 `data/episode_0..N`，manifest 和 group attrs 持久化 `source_episode_index -> zarr_episode_index` 映射。追加 Episode 时先在 `.staging/` 完整构建，成功后才原子挂入 `data/episode_N`；失败不会改动已有 Episode。`cleanup_intermediates` 已实现为 Episode 级可选清理，当前默认是 `false`。

## 常用命令

启动 GUI 并连接三路采集：

```bash
python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8899 \
  --no-open \
  --coinft-port /dev/cu.usbmodem183837501 \
  --coinft-simulate
```

只运行 iPhone V2 receiver：

```bash
python3 entrypoints/iphone_stream_receiver.py --help
```

检查 D435 sudo 安装入口：

```bash
bash entrypoints/install_sudoers_macos.sh --help
```

## 当前可验证状态

已经验证过的链路事实：

- iPhone usbmux 能识别设备并接收数据。
- D435 通过一次性 sudoers 安装后，可由主链路无密码启动采集控制。
- Teensy `/dev/cu.usbmodem183837501` 的模拟 CoinFT-like stream 可正常进入 CoinFT buffer。
- GUI 的 `/state` 能汇总 `capture_summary`，三路状态可由 GUI 读取。
- 本轮 live smoke 验证 `capture_summary.ready=true`，iPhone / D435 / CoinFT 三路均 `ok=true`。
- `umift-replay-buffer-zarr` 已在 `runs/run_20260626_124555` 上生成 `acp_replay_buffer_gripper.zarr`：416 个 RGB/pose/gripper 样本、2773 个双路 CoinFT wrench 样本、416 个 D435 global RGB/depth 样本。
- 生成的 UMI-FT Zarr 已通过 `external UMI-FT source tree/PyriteML/diffusion_policy/common/replay_buffer.py::ReplayBuffer.copy_from_store` 读取检查。

重构后的基本检查：

```bash
python3 -m py_compile entrypoints/receiver_web_gui.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q -p no:cacheprovider
```
