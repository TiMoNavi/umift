# UMIFT DataCollect

这是一个新的、独立的数据采集工程。目标是先用 Mac / Windows 笔记本作为采集协调器，完成多路传感器数据收集、时间轴对齐和训练数据导出。

当前设计不要求 iPhone 直接连接 CoinFT，也不要求 CoinFT 通过网络或蓝牙进入 iPhone。所有关键同步先回到笔记本端完成，优先追求最快得到真实、可检查、可转换的数据。

## 先看这里

现在项目已经整理成“按模块开工”的入口。如果你是按部分推进，不想先读完整套总文档，建议直接从这里进入：

```text
modules/
├── 01_global_camera
├── 02_iphone_gripper
├── 03_coinft_teensy
└── 04_laptop_alignment_export
```

对应入口：

- `modules/01_global_camera/README.md`
- `modules/02_iphone_gripper/README.md`
- `modules/03_coinft_teensy/README.md`
- `modules/04_laptop_alignment_export/README.md`

当前额外约束：

- `modules/04_laptop_alignment_export/` 也是 Mac 端运行的软件工作区
- 新增的 Mac 侧协调 / 同步脚本优先放在这个目录

每个模块 README 都会写清楚：

- 这个模块只负责什么
- 输入什么
- 输出什么
- 当前状态
- 当前阻塞
- 不需要关心什么

如果你对“还有其他文件夹是干嘛的”感到困惑，直接看：

- `DIRECTORY_MAP.md`
- `docs/README.md`
- `reference/README.md`
- `tools/README.md`
- `schemas/README.md`

## 系统总览

```text
外置全局深度摄像头 --USB--> 笔记本
夹爪 iPhone --------有线--> 笔记本
2 x CoinFT -> Teensy --USB--> 笔记本

笔记本:
  1. 采集全局 RGB/depth
  2. 拉取或接收 iPhone 夹爪 RGB/depth/6DoF pose
  3. 采集 CoinFT raw/wrench
  4. CV 识别夹爪开合度
  5. 对齐多路时间轴
  6. 转换成 ForceFlow 对齐的 Zarr 训练格式
```

## 四路职责

### 1. 外置场景全局摄像头

连接方式：

```text
USB -> Mac / Windows 笔记本
```

型号：

```text
Intel RealSense D435
```

职责：

- 记录场景全局 RGB 视频。
- 记录场景全局 depth。
- 提供第三方观察视角，辅助检查任务、遮挡、夹爪接触和 CoinFT 事件。

第一版要求：

- 能被 librealsense / RealSense SDK 2.0 读取。
- 每帧记录笔记本接收时间戳。
- RGB 和 depth 尽量同源同步；如果设备 SDK 提供硬件时间戳，也一并保存。

输出草案：

```text
global_camera/
├── rgb.mp4
├── depth.raw or depth.zarr
├── frame_timestamps.csv
└── camera_metadata.json
```

### 2. 夹爪上的 iPhone

连接方式：

```text
iPhone --USB/USB-C cable--> Mac / Windows 笔记本
```

职责：

- 记录夹爪视角 RGB。
- 记录夹爪视角 depth。
- 记录夹爪 / 手机的 6DoF pose。
- 输出 UMI/UMI-FT 兼容风格的 demo 原始文件。

第一版策略：

- iPhone App 仍然独立录制。
- 录制结束后，笔记本通过有线连接拉取 App container 或用户导出目录。
- iPhone 的 `poseTimes` 作为夹爪视角主时间序列。
- iPhone 不负责 CoinFT 同步；CoinFT 同步在笔记本端完成。

输出草案：

```text
iphone_gripper/
└── <date>/<session>_demonstration/
    ├── left.json or right.json
    ├── *_rgb.mp4
    ├── *_depth.raw
    ├── *_ultrawidergb.mp4
    └── metadata files
```

### 3. Teensy + 两个 CoinFT

连接方式：

```text
CoinFT left/right -> Teensy 4.x -> USB serial -> Mac / Windows 笔记本
```

职责：

- Teensy 读取左右 CoinFT 原始 12 通道。
- 笔记本读取 Teensy USB serial。
- 笔记本执行 tare、标定、ONNX/norm 推理，得到左右 6D wrench。
- 笔记本保存 UMI-FT 风格 CoinFT CSV。

当前硬件状态：

```text
Teensy 4.1，无应用固件，需要先刷写 CoinFT bridge 固件。
```

当前 Teensy 4.1 bridge 协议：

```text
Host -> Teensy:
i = idle
s = start streaming
t = tare

Teensy -> Host（两路在线或模拟模式）:
0x00 0x00
+ sequence_id uint32 little-endian
+ teensy_time_us uint32 little-endian
+ left 24-byte frame body
+ right 24-byte frame body
total: 58 bytes
baud: 115200

运行时会自动识别 0/1/2 路 CoinFT：0 路输出模拟 58-byte 双路包，1 路输出
24-byte 单路调试 body，2 路输出上述 58-byte 双路包。生产采集使用 2 路协议。
```

输出草案：

```text
coinft/
├── raw_coinft_stream.csv
├── UMIFT_data_<YYMMDD_HHMMSS>_<session>_LF.csv
└── UMIFT_data_<YYMMDD_HHMMSS>_<session>_RF.csv
```

UMI-FT 风格列：

```text
Timestamp,Fx,Fy,Fz,Mx,My,Mz,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12
```

### 4. 笔记本本体

笔记本是本阶段的 coordinator，不只是文件搬运工具。

职责：

1. 识别 iPhone 画面中的夹爪开合度。
2. 对齐全局摄像头、iPhone、CoinFT 的时间轴。
3. 维护 run 目录、metadata 和日志。
4. 转换成训练需要的最终格式。

夹爪开合度第一版策略：

- 输入：iPhone 夹爪视角 RGB 视频。
- 方法：允许贴 marker，优先使用 ArUco/AprilTag 这类通用 marker 方案。
- 输出：每个 iPhone frame 对应一个 `gripper_width_m`。
- 如果视觉估计不稳定，先输出诊断置信度，不硬塞错误值。

输出草案：

```text
gripper_width/
├── gripper_width_by_frame.csv
├── gripper_width_debug.mp4
└── gripper_width_report.json
```

## 时间轴与对齐

本阶段采用笔记本端统一对齐。每一路都保留自己的原始时间戳，并在对齐阶段生成统一目标时间轴。

候选主时间轴：

```text
A. iPhone poseTimes
B. 全局摄像头 frame timestamps
C. 固定频率训练时间轴
```

当前建议：

- 第一版以 `iPhone poseTimes` 为主时间轴，因为夹爪 6DoF pose 和夹爪视角图像天然绑定。
- CoinFT 插值到 iPhone poseTimes。
- 全局摄像头按最近帧或插值索引映射到 iPhone poseTimes。
- 训练格式出来后，再决定是否重采样到固定频率。

对齐原则：

```text
raw data 不改
alignment 输出新文件
所有 offset / latency / dropped frames 写入 metadata
```

第一版同步来源：

- iPhone：设备内记录的 ISO 时间 / poseTimes。
- CoinFT：笔记本收到串口包时的系统时间。
- 全局摄像头：笔记本收到帧时的系统时间，若 SDK 有硬件时间戳则额外保存。

## 录制触发与有效片段

所有设备先完整录制；有效训练片段由夹爪开合度后处理决定。

开始规则：

```text
第一次检测到夹爪接近闭合
-> 3 秒倒计时
-> 倒计时结束时刻作为 effective_start_time
```

结束规则：

```text
effective_start_time 之后
iPhone 6DoF pose 连续 3 秒抖动很小
-> 判定操作者拿稳夹爪
-> effective_end_time
```

这样不需要实时同时触发 D435、iPhone 和 CoinFT，也不依赖人按停止按钮。原始数据完整保留；训练导出只裁剪 `[effective_start_time, effective_end_time]`。

为避免任务中自然停顿被误判为结束，第一版应设置最小有效时长，例如 `min_episode_duration_s = 5-10`，并在对齐报告中记录结束触发窗口。

## 坐标系重置

iPhone App 需要提供重置坐标系的步骤：

```text
按下 Reset Origin / Record 按钮
当前 iPhone pose 作为 session world 原点
摄像头朝向作为正方向
```

建议定义：

```text
T_world_phone_at_reset = Identity
T_world_phone(t) = inverse(T_arkit_phone_at_reset) * T_arkit_phone(t)
```

这对 iPhone 轨迹是可行的。但它只定义 iPhone/ARKit 的 session world frame，不自动等于 D435 全局相机坐标系。若训练格式要求多相机统一空间坐标，需要后续做 D435 到 session world 的外参标定。

后续可升级：

- 笔记本向 iPhone App 发开始/停止控制。
- 录制开头做可见触碰动作，用于估计整体 offset。
- 使用音频/LED/屏幕闪烁作为跨设备同步事件。
- 如果硬件支持，加入 PTP/NTP/SDK hardware timestamp。

## Run 目录结构

每次采集生成一个 run：

```text
runs/<YYYYMMDD_HHMMSS>_<session>/
├── global_camera/
│   ├── rgb.mp4
│   ├── depth.raw
│   ├── frame_timestamps.csv
│   └── camera_metadata.json
├── iphone_gripper/
│   └── ... pulled iPhone demonstration ...
├── coinft/
│   ├── raw_coinft_stream.csv
│   ├── UMIFT_data_*_LF.csv
│   └── UMIFT_data_*_RF.csv
├── gripper_width/
│   ├── gripper_width_by_frame.csv
│   └── gripper_width_report.json
├── aligned/
│   ├── aligned_index.csv
│   ├── aligned_coinft.csv
│   ├── aligned_global_camera.csv
│   └── ALIGNMENT_REPORT.md
├── export/
│   └── <task>.zarr/
├── logs/
└── RUN_METADATA.json
```

## 阶段计划

### Phase A：最小可运行闭环

目标：先采到一组真实 iPhone + CoinFT + 全局摄像头数据。

任务：

- 建立 run 目录。
- iPhone 继续用现有 App 录制并拉取。
- CoinFT 用 Teensy USB serial 在笔记本采集。
- 全局摄像头用最小脚本采 RGB/depth。
- 生成基础时间范围报告。

### Phase B：笔记本端对齐

目标：把 CoinFT 和全局摄像头映射到 iPhone poseTimes。

任务：

- 解析 iPhone poseTimes。
- 解析 CoinFT LF/RF CSV。
- 解析全局摄像头 frame timestamps。
- 输出 aligned index 和 nearest sample age。
- 生成对齐报告。

### Phase C：夹爪开合度视觉估计

目标：从 iPhone 夹爪视角得到 gripper width。

任务：

- 明确夹爪 marker / 视觉特征方案。
- 输出逐帧 `gripper_width_m`。
- 输出 debug video 和置信度。

### Phase D：训练格式导出

目标：导出与 ForceFlow / iffyuan-XArm-Toolkit 对齐的 Zarr 训练数据。

任务：

- 使用 `schemas/forceflow_zarr_schema.md` 作为硬约束。
- 从 aligned 数据生成 `export/<task>.zarr`。
- 生成 `<task>_normalizer.json`。
- 运行 ForceFlow validator。
- 保留原始数据到训练数据的 provenance。

格式要求见：

```text
docs/training_zarr_alignment.md
docs/training_format_implementation.md
schemas/forceflow_zarr_schema.md
tools/export/README.md
```

## 当前风险

- Mac / Windows 对 iPhone 有线拉取方式不同；Mac 优先，Windows 需要后续单独验证。
- CoinFT 6D wrench 依赖真实 ONNX/norm 标定文件；没有标定时只能做链路验证。
- D435 在 Mac 上的 SDK 稳定性需要实测；Windows 通常更省心。
- 多设备绝对时间可能存在 offset，第一版要靠明显同步事件检查。
- ForceFlow 训练格式已作为第一版导出目标；后续若增加 LeRobot/OpenPI 等格式，应作为额外 exporter，不替代 Zarr 主路径。

## 当前目录

```text
modules/
  按模块分工的工作入口。推荐从这里开始看。

docs/
  全局设计、流程、metadata、训练格式规则。

reference/
  外部资料、历史实现、硬件参考来源。

tools/
  真实脚本实现。

schemas/
  输出格式硬约束。

runs/
  真实采集输出，不建议提交到 git。
```
