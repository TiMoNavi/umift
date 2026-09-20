# 模块 01：全局摄像头

## 模块目标

用 `Intel RealSense D435 / D435i` 采集全局场景的 RGB / Depth，并把逐帧时间戳和相机元数据保存到 run 目录。

当前你手里是 `D435i`，第一版按 `D435` 兼容机型处理：

- 使用 RGB
- 使用 Depth
- 不使用 IMU

## 这个模块只负责什么

1. 连接相机
2. 启动 RGB / Depth 采集
3. 保存视频或帧数据
4. 保存逐帧时间戳
5. 保存相机参数和运行配置

## 这个模块暂时不负责什么

- 不负责 iPhone
- 不负责 CoinFT
- 不负责训练格式
- 不负责空间外参
- 不负责全局时间轴融合

它只需要把自己的原始数据干净落盘。

## 输入

### 来自操作者

- USB 连接好的 `D435i`
- 本次 run 的 `run_id`
- 采集参数：
  - RGB 分辨率
  - RGB FPS
  - Depth 分辨率
  - Depth FPS

### 来自项目约束

- depth 必须录
- 逐帧时间戳必须保存
- 元数据必须写进 `RUN_METADATA.json`

## 输出

写入：

```text
runs/<run_id>/global_camera/
├── rgb.mp4
├── depth.raw or depth.zarr
├── frame_timestamps.csv
└── camera_metadata.json
```

其中至少要有：

- `rgb.mp4`
- `frame_timestamps.csv`
- 一份可解析的 depth 数据

## 输出字段要求

### `frame_timestamps.csv`

至少包含：

```text
frame_index
host_receive_time_s
realsense_frame_timestamp_ms
realsense_timestamp_domain
```

如果脚本当前做不到这么全，至少先保证：

```text
frame_index
host_receive_time_s
```

## 对外接口

这个模块交给后续模块的东西只有三类：

1. `rgb.mp4`
2. depth 原始数据
3. `frame_timestamps.csv`

后面的对齐模块只依赖这些，不该要求这个模块理解 iPhone 或 CoinFT 细节。

## 当前状态

- 设备型号已知：`D435i`
- 路线已定：先按 `D435` 兼容使用
- IMU 明确先忽略
- 还没定死最终分辨率 / FPS

## 当前阻塞

1. 旧电脑上 `librealsense` 稳定性要实测
2. depth 存储格式还没最终定死
3. 最终推荐参数要靠短录制测试

## 参考入口

- `reference/intel_realsense_d435/README.md`
- `docs/system_architecture.md`
- `docs/run_metadata_schema.md`
- `DIRECTORY_MAP.md`

## 现在可直接用的实现

当前模块已经补了一个最小可用的 D435 / D435i 采集脚本：

```bash
python3 modules/01_global_camera/capture_d435i.py --help
```

另有一个给这台 Mac 准备的 wrapper，会自动处理：

- 使用统一控制入口
- 检查是否已启用 passwordless sudo
- 未启用时自动退回普通 sudo
- 给常用动作提供短命令

入口：

```bash
bash modules/01_global_camera/run_capture_macos.sh --help-wrapper
```

这个 wrapper 现在支持短命令：

- `warmup-sudo`
- `install-passwordless-sudo`
- `list`
- `stream`
- `test`
- `start`
- `stop`
- `status`
- `capture`

另外，模块里现在还有一个更底层、适合 sudo 白名单的固定入口：

```bash
sudo bash modules/01_global_camera/control_capture_macos.sh help
```

这个入口适合后续做：

- `sudoers` 白名单
- 自动化脚本调用
- 固定 `start / stop / status / list / run-once` 动作

推荐的一次性设置命令：

```bash
bash modules/01_global_camera/run_capture_macos.sh install-passwordless-sudo
```

它会做两件事：

1. 把 RealSense 控制脚本、采集脚本、本地 `librealsense` 运行时安装到 root-owned 目录：

```text
/usr/local/libexec/umift-global-camera
```

2. 写入一个很窄的 sudoers 白名单，只放行这个固定入口：

```text
/usr/local/libexec/umift-global-camera/control_capture_macos.sh
```

这样后续 `list / test / start / stop / status / capture` 都可以从 wrapper 直接走，不需要你反复输密码。

## 固化后的推荐脚本

日常只用这 4 个脚本：

- `capture_d435i.py`
  - Python 主实现
  - 同时支持“录制模式”和“实时调试流模式”
- `run_capture_macos.sh`
  - 人工调试入口
  - 最少输入，自动走 sudo 白名单
- `control_capture_macos.sh`
  - 固定控制入口
  - 给自动化和 sudo 白名单用
- `install_sudoers_macos.sh`
  - 一次性安装 passwordless sudo

## 固化后的调试动作

### 1. 看设备能不能被拿到

```bash
bash modules/01_global_camera/run_capture_macos.sh list
```

用途：

- 确认 D435i 已连接
- 确认当前 serial 仍然是 `327122071246`
- 确认 librealsense 调用链是通的

### 2. 看实时流是不是正在回来

```bash
bash modules/01_global_camera/run_capture_macos.sh stream
```

默认行为：

- 前台运行 10 秒
- 实时打印一段 JSON 状态
- 持续刷新：

```text
/tmp/realsense_stream_preview/latest_rgb.jpg
/tmp/realsense_stream_preview/latest_depth.png
```

你可以用它确认三件事：

- 设备能被真正打开
- RGB / Depth 帧在持续返回
- 不是只会录制，实时数据流本身也在正常工作

自定义时长和预览目录：

```bash
bash modules/01_global_camera/run_capture_macos.sh stream 15 /tmp/realsense_live
```

### 3. 做一次最小录制验证

```bash
bash modules/01_global_camera/run_capture_macos.sh test
```

默认输出：

```text
/tmp/realsense_test
```

适合确认：

- `rgb.mp4`
- `depth.raw`
- `frame_timestamps.csv`
- `camera_metadata.json`
- `capture_log.json`

是否都能稳定落盘。

### 4. 做前台正式录制

```bash
bash modules/01_global_camera/run_capture_macos.sh capture \
  --output-dir /tmp/realsense_capture_manual \
  --duration-s 20
```

### 5. 做后台长录制

启动：

```bash
bash modules/01_global_camera/run_capture_macos.sh start \
  --output-dir /tmp/realsense_bg \
  --duration-s 60
```

查看状态：

```bash
bash modules/01_global_camera/run_capture_macos.sh status
```

结束：

```bash
bash modules/01_global_camera/run_capture_macos.sh stop
```

状态文件位置：

```text
modules/01_global_camera/.runtime/
```

其中包含：

- `capture.pid`
- `capture_session.json`
- `capture.stdout.log`

## Python 主脚本的两种模式

### 录制模式

```bash
python3 modules/01_global_camera/capture_d435i.py \
  --output-dir /tmp/realsense_capture \
  --duration-s 10
```

特点：

- 落盘 `rgb.mp4`
- 落盘 `depth.raw`
- 落盘逐帧时间戳
- 落盘相机 metadata

### 实时流模式

```bash
python3 modules/01_global_camera/capture_d435i.py \
  --stream \
  --duration-s 10 \
  --preview-dir /tmp/realsense_stream_preview
```

特点：

- 不写 `rgb.mp4` / `depth.raw`
- 直接输出实时状态 JSON
- 持续刷新最新 RGB / depth 预览图

这就是当前“确认能调用摄像头、实时返回数据流”的主调试入口。

### 推荐职责分层

- `run_capture_macos.sh`：给人用，少打字
- `control_capture_macos.sh`：源码版控制脚本
- `/usr/local/libexec/umift-global-camera/control_capture_macos.sh`：安装后的 root-owned 固定入口，给 sudo 白名单和自动化用
- `install_sudoers_macos.sh`：一次性安装 passwordless sudo

它会输出到：

```text
runs/<run_id>/global_camera/
├── rgb.mp4
├── depth.raw
├── frame_timestamps.csv
├── camera_metadata.json
└── capture_log.json
```

其中：

- `depth.raw` 是逐帧顺序写入的 `uint16` 深度图
- shape / dtype / depth scale 都写进 `camera_metadata.json`
- `frame_timestamps.csv` 已包含 host 时间和 RealSense 硬件时间

## 推荐用法

如果 run 目录已经由 `tools/capture/sync_run_data.py init-run` 创建：

```bash
python3 modules/01_global_camera/capture_d435i.py \
  --run-dir runs/<run_id> \
  --duration-s 10
```

如果只是先独立测相机：

```bash
python3 modules/01_global_camera/capture_d435i.py \
  --output-dir /tmp/realsense_test \
  --duration-s 10
```

列出当前 librealsense 能看到的设备：

```bash
python3 modules/01_global_camera/capture_d435i.py --list-devices
```

## 当前机器的实际情况

这台 Mac 目前已经完成过一次真实短录制验证。

已确认：

- USB 层能看到设备
- `librealsense` / `pyrealsense2` 已编译可用
- 用 `sudo` 运行时，`rs-enumerate-devices` 能正常列出设备
- 用 `sudo` 运行时，`capture_d435i.py` 已成功录制 5 秒测试数据

这台机器上当前应优先使用的 RealSense serial 是：

- `327122071246`

注意：

- `243323065541` 常见于 `Asic Serial Number / Firmware Update Id`
- 真正给 `librealsense` / `pyrealsense2` 指定设备时，优先使用 `327122071246`

### macOS 上的真实限制

这台 Mac 上，普通用户态运行 `librealsense` 会被 macOS 拦住，典型报错是：

```text
RS2_USB_STATUS_ACCESS
failed to set power state
```

但使用 `sudo` 后已经验证通过。

因此当前结论不是“Mac 不能用 D435i”，而是：

```text
macOS + librealsense 这条链路在这台机器上需要 sudo
```

但这个 sudo 现在已经可以通过一次性白名单安装，收敛成长期自动化入口。

## macOS 安装入口

项目里补了一个安装 helper：

```bash
bash modules/01_global_camera/install_realsense_macos.sh
```

它会：

1. clone / update `librealsense`
2. 用本机 `python3` 构建 Python wrapper
3. 安装 CLI 到模块内的 `third_party/librealsense-install`
4. 提示你如何设置 `PATH` 和 `PYTHONPATH`

如果这台老 Mac 上后续长录制不稳定，仍然建议遵循项目原来的策略：

- Mac 继续承担 iPhone / 总协调
- Windows 负责 D435 / CoinFT 采集

## 这台机器上已验证可用的命令

### 1. 列设备

推荐直接用 wrapper：

```bash
bash modules/01_global_camera/run_capture_macos.sh list
```

等价底层命令：

```bash
sudo env \
  DYLD_LIBRARY_PATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/lib \
  PYTHONPATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/python \
  python3 modules/01_global_camera/capture_d435i.py --list-devices
```

### 2. 5 秒短录制

推荐直接用 wrapper：

```bash
bash modules/01_global_camera/run_capture_macos.sh test
```

或者自定义时长和输出目录：

```bash
bash modules/01_global_camera/run_capture_macos.sh test 8 /tmp/d435_test
```

等价底层命令：

```bash
sudo env \
  DYLD_LIBRARY_PATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/lib \
  PYTHONPATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/python \
  python3 modules/01_global_camera/capture_d435i.py \
    --output-dir /tmp/realsense_test \
    --serial 327122071246 \
    --duration-s 5
```

实测结果：

- 输出目录：`/private/tmp/realsense_test`
- `frameCount`: `113`
- `depthBytes`: `69427200`
- 已成功写出：
  - `rgb.mp4`
  - `depth.raw`
  - `frame_timestamps.csv`
  - `camera_metadata.json`
  - `capture_log.json`

### 3. 接入 run 目录录制

推荐直接用 wrapper：

```bash
bash modules/01_global_camera/run_capture_macos.sh capture \
  --run-dir runs/<run_id> \
  --duration-s 10
```

### 4. 后台开始 / 结束一段录制

开始：

```bash
bash modules/01_global_camera/run_capture_macos.sh start \
  --run-dir runs/<run_id> \
  --duration-s 30
```

查看状态：

```bash
bash modules/01_global_camera/run_capture_macos.sh status
```

结束：

```bash
bash modules/01_global_camera/run_capture_macos.sh stop
```

### 5. 先手动刷新一次 sudo 凭据

如果你准备连续测试多次，可以先执行：

```bash
bash modules/01_global_camera/run_capture_macos.sh warmup-sudo
```

这样当前 sudo ticket 会先被刷新，后面的 wrapper 调用更顺手。

## 为 sudo 白名单准备的固定入口

如果后续要配置 `sudoers` 白名单，建议白名单只放这一条固定入口：

```bash
/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/control_capture_macos.sh
```

而不是对白名单放开任意 `python3` 或任意 shell。

这个固定入口支持：

- `list`
- `run-once`
- `start`
- `stop`
- `status`

这样后续自动化只需要触发这个入口，就能完成全局相机的“开始 / 结束”控制。

等价底层命令：

```bash
sudo env \
  DYLD_LIBRARY_PATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/lib \
  PYTHONPATH=/Users/550m/code/UMIFT-datacollect/modules/01_global_camera/third_party/librealsense-install/python \
  python3 modules/01_global_camera/capture_d435i.py \
    --run-dir runs/<run_id> \
    --serial 327122071246 \
    --duration-s 10
```

如果 `runs/<run_id>/RUN_METADATA.json` 已存在，脚本会自动回写：

- `hardware.global_camera`
- `outputs.global_camera_dir`

## 已知边界

- 第一版仍然不采 IMU
- depth 当前先存 `depth.raw`，还没有切到 `zarr`
- `rgb.mp4` 依赖本机 OpenCV 可用的视频编码器；如果 `mp4v` 不可用，脚本会直接报错
- 如果 `--run-dir` 下已经有 `RUN_METADATA.json`，脚本会自动回写 `hardware.global_camera` 和 `outputs.global_camera_dir`
- 这台 Mac 上当前必须用 `sudo` 跑 `librealsense` / `pyrealsense2` 采集
- `run_capture_macos.sh` 只能减少你每次手工敲长命令的痛苦，第一次仍然需要输入一次 sudo 密码
