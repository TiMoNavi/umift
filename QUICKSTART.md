# UMIFT DataCollect — Quick Start

这份文档回答一个问题：**解压之后怎么跑起来**。完整的架构说明、数据格式和操作细节见 `docs/` 和各模块 `README.md`。

---

## 1. 环境要求

- macOS（Apple Silicon 或 Intel 均可）
- Xcode + Command Line Tools（iPhone 部署和 RealSense 控制脚本需要）
- Python 3.9+，需要以下包：

```bash
pip install av opencv-python numpy pyserial zarr
# 运行测试还需要：
pip install pytest
```

推荐用 conda 管理环境：

```bash
conda create -n umift python=3.9
conda activate umift
pip install av opencv-python numpy pyserial zarr pytest
```

- iPhone 15 Pro 或更新（开启开发者模式，信任 Mac）
- Intel RealSense D435 / D435i
- Teensy 4.1 + 两路 CoinFT

---

## 2. 解压后的基本检查

```bash
cd UMIFT_datacollect_source_20260812
export UMIFT_ROOT="$PWD"

# 确认项目文件完整
test -f modules/04_laptop_alignment_export/entrypoints/receiver_web_gui.py \
  && echo "project files: ok"

# 确认 Python 依赖
python3 -c 'import av, cv2, numpy, serial, zarr; print("python dependencies: ok")'
```

---

## 3. 启动主 GUI（最常用的命令）

进入 module04 目录，启动浏览器端采集控制台：

```bash
cd "$UMIFT_ROOT/modules/04_laptop_alignment_export"

python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8765 \
  --coinft-port /dev/cu.usbmodemXXXXXX \
  --coinft-config "$UMIFT_ROOT/modules/03_coinft_teensy/configs/coinft_04_laptop_calibrated.json"
```

浏览器打开 `http://127.0.0.1:8765/`，可以看到三路设备状态、实时预览和录制控制。

常用选项：

| 选项 | 说明 |
|------|------|
| `--port` | HTTP 监听端口，默认 8765 |
| `--coinft-port` | CoinFT Teensy 串口，如 `/dev/cu.usbmodemXXXXXX`（用 `ls /dev/cu.usbmodem*` 查找） |
| `--coinft-config` | CoinFT 标定配置文件路径 |
| `--coinft-source none` | 无 CoinFT 硬件时跳过 CoinFT（纯 iPhone + D435 模式） |
| `--no-auto-start` | 启动后不自动开始采集，手动在浏览器里 Arm |
| `--no-open` | 不自动打开浏览器 |

完整首次部署说明（D435 sudoers 安装、iPhone App 部署、Dataset 创建）见：

```
modules/04_laptop_alignment_export/docs/01_operator_guide.md
```

---

## 4. 命令行采集（不用浏览器）

GUI 必须先在后台运行（见第 3 步），然后用 `capture_cli.py` 在纯命令行下控制采集。不需要额外依赖，只用标准库。

**启动监控（arm all + 持续输出状态）：**

```bash
cd modules/04_laptop_alignment_export

python3 entrypoints/capture_cli.py run --port 8765
```

脚本执行流程：
1. 连接 GUI，确认可达
2. 发送 `arm_all`，启动 iPhone + D435 + CoinFT 三路采集
3. 逐行打印尚未就绪的原因，最长等待 60 秒
4. 就绪后每秒输出一行状态，持续监控

**录制由 iPhone 控制**：iPhone 按下录制时状态自动从 `IDLE` 变为 `REC`，停止时变回 `IDLE`。不需要在 Mac 侧再发任何命令。

状态行示例：

```
[03:45:12] IDLE  iPhone 30 fps / 14 Mbps  CoinFT L Fx+0.12 Fy-0.03 Fz+1.20N  R Fx-0.05 ...  frames=1823  gate:ready
[03:45:45] REC   iPhone 30 fps / 14 Mbps  CoinFT L Fx+0.12 Fy-0.03 Fz+1.20N  R Fx-0.05 ...  frames=2841  gate:ready  ep=3
```

字段说明：`IDLE` 就绪待录，`ARM` 已 arm 但设备还没全就绪，`REC` 正在录制，`ep=N` 当前 episode 序号。CoinFT 已标定时显示 Fx/Fy/Fz（单位 N），未标定时显示 raw ADC 均值。

**停止整套采集（Ctrl-C 或另开终端）：**

```bash
# 方式一：在监控终端按 Ctrl-C（自动发送 stop 命令）

# 方式二：另开终端
python3 entrypoints/capture_cli.py stop --port 8765
```

**监控 + 每次录制结束后自动 normalize → align → export：**

```bash
python3 entrypoints/capture_cli.py run --port 8765 --auto-process
```

每次 iPhone 停止一段录制，脚本自动对该 episode 依次执行：

```
normalize_run.py  →  align_run.py  →  export_run.py
```

如需指定导出配置：

```bash
python3 entrypoints/capture_cli.py run --port 8765 --auto-process \
  --export-config config/export_profiles/umift_replay_buffer_v0.json
```

**GUI 没有运行时的错误：**

```
Connecting to 127.0.0.1:8765…
ERROR: cannot reach GUI at 127.0.0.1:8765 — ...
Make sure receiver_web_gui.py is running first.
```

**退出码：** `0` 正常停止，`1` 连接失败或超时，`2` Ctrl-C 中断（stop 已自动发送）。

完整选项：`python3 entrypoints/capture_cli.py --help`

---

## 4. 数据后处理命令

采集完成后，在 module04 目录下执行以下步骤。`<run_dir>` 是 `runs/` 下的具体录制目录，如 `runs/run_20260916_193307_dataset_2026-09-16`。

### 4.1 归一化（raw → normalized JSONL）

```bash
cd modules/04_laptop_alignment_export

python3 entrypoints/normalize_run.py <run_dir>
```

### 4.2 时间轴对齐（normalized → aligned）

```bash
python3 entrypoints/align_run.py <run_dir>
```

### 4.3 导出训练 Zarr（aligned → UMI-FT replay buffer）

导出单个 episode：

```bash
python3 entrypoints/export_run.py <run_dir> <episode_index> \
  --format umift-replay-buffer-zarr \
  --config config/export_profiles/umift_replay_buffer_v0.json
```

批量导出所有 episode：

```bash
python3 entrypoints/export_run.py <run_dir> --all \
  --format umift-replay-buffer-zarr \
  --config config/export_profiles/umift_replay_buffer_v0.json
```

也可以在浏览器 GUI 里点击 Normalize → Align → Export Selected Format，效果相同。

### 4.4 检查 Zarr 数据

启动 Zarr 检查器（浏览器端）：

```bash
python3 entrypoints/zarr_inspector_web.py <zarr_path>
```

---

## 5. RealSense D435 单独运行

```bash
# 列出可用设备
bash modules/01_global_camera/run_capture_macos.sh list

# 测试相机
bash modules/01_global_camera/run_capture_macos.sh test

# 首次安装 sudoers（只需一次，需要管理员密码）
sudo bash modules/04_laptop_alignment_export/entrypoints/install_sudoers_macos.sh
```

---

## 6. CoinFT 标定配置

左右传感器已预配置在：

```
modules/03_coinft_teensy/configs/coinft_04_laptop_calibrated.json
```

对应的 ONNX 模型和 norm 文件已包含在：

```
modules/03_coinft_teensy/assets/calibration/production/
```

如果更换了 CoinFT 硬件，需要重新标定并更新该 JSON。标定流程见：

```
modules/03_coinft_teensy/steps/02_coinft_calibration/README.md
```

---

## 7. 开发自检

```bash
cd modules/04_laptop_alignment_export

# 语法检查
python3 -m py_compile entrypoints/receiver_web_gui.py

# 运行单元测试（需要 pytest）
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q
```

---

## 8. 目录速查

```text
UMIFT_datacollect_source_20260812/
├── QUICKSTART.md                        ← 你在看这里
├── README.md                            ← 项目概述
├── DIRECTORY_MAP.md                     ← 完整目录地图
├── 交付说明.md                           ← 交付范围、环境和注意事项
├── modules/
│   ├── 01_global_camera/               ← RealSense D435 采集
│   ├── 02_iphone_gripper/              ← iPhone App 源码和部署
│   ├── 03_coinft_teensy/               ← CoinFT 固件、标定模型、配置
│   └── 04_laptop_alignment_export/     ← 主 GUI、对齐、导出（主要工作区）
│       ├── entrypoints/                ← 所有可执行入口
│       ├── docs/                       ← 操作指南和技术文档
│       └── config/export_profiles/    ← 导出格式配置
├── docs/                               ← 全局架构和操作文档
├── schemas/                            ← 数据格式定义
└── runs/                               ← 录制数据（运行时生成，不含在包内）
```

---

## 文档导航

| 目标 | 文档 |
|------|------|
| 首次部署完整流程 | [docs/01_operator_guide.md](modules/04_laptop_alignment_export/docs/01_operator_guide.md) |
| 主链路架构 | [docs/02_runtime_architecture.md](modules/04_laptop_alignment_export/docs/02_runtime_architecture.md) |
| 数据格式定义 | [docs/03_data_contract.md](modules/04_laptop_alignment_export/docs/03_data_contract.md) |
| 导出格式和字段 | [docs/04_export_contract.md](modules/04_laptop_alignment_export/docs/04_export_contract.md) |
| 故障排除 | [docs/05_troubleshooting.md](modules/04_laptop_alignment_export/docs/05_troubleshooting.md) |
| 采集流程概述 | [docs/collection_workflow.md](docs/collection_workflow.md) |
| 系统架构 | [docs/system_architecture.md](docs/system_architecture.md) |
