# 安装与环境排错

所有命令从交付包根目录执行。推荐 **64 位 CPython 3.11**，也支持 3.12；请勿使用系统 Python 3.9 或 Python 3.13+ 安装这份固定版本清单。

## Windows（PowerShell）

先安装 python.org 的 64 位 Python 3.11，再进入解压目录：

```powershell
cd C:\work\UMIFT_datacollect_source_20260812
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
winget install --id Gyan.FFmpeg --exact
```

安装 FFmpeg 后重开 PowerShell，重新进入项目目录，再运行：

```powershell
ffmpeg -version
.\.venv\Scripts\python.exe tools/check_environment.py
.\.venv\Scripts\python.exe modules/04_laptop_alignment_export/entrypoints/receiver_web_gui.py --host 127.0.0.1 --port 8765 --no-open --no-auto-start
```

浏览器打开 http://127.0.0.1:8765/ 。以上命令不用激活环境，不受 PowerShell 的 Activate.ps1 执行策略影响。只检查网页、暂不需要视频预览时，可省略 FFmpeg 安装，并用 `tools/check_environment.py --skip-ffmpeg` 检查。

Teensy 串口要填实际的 `COM3` 等名称；可运行 `.\.venv\Scripts\python.exe -m serial.tools.list_ports -v` 查看。页面使用 `--coinft-port COM3` 指定串口，换电脑后不要照抄 macOS 的 `/dev/cu.*`。

## macOS（终端）

安装 Python 3.11（例如 Homebrew），在项目根目录运行：

```bash
brew install python@3.11 ffmpeg
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
python tools/check_environment.py
python modules/04_laptop_alignment_export/entrypoints/receiver_web_gui.py --host 127.0.0.1 --port 8765 --no-open --no-auto-start
```

浏览器打开 http://127.0.0.1:8765/ 。以后每次开终端先进入项目目录并 `source .venv/bin/activate`，也可以直接调用 `.venv/bin/python`。

确认页面可用后，再按 [采集操作指南](../modules/04_laptop_alignment_export/docs/01_operator_guide.md) 配置硬件。iPhone App 的编译部署需要完整 Xcode；D435 使用仓库提供的 macOS SDK 构建和 sudoers 安装脚本，这些系统组件不由 pip 安装。

## 支持范围和依赖分组

| 用途 | 安装入口 / 额外要求 |
| --- | --- |
| 网页后端、CoinFT 串口及 ONNX 推理、标记视觉、normalize/align/Zarr 导出 | 根目录 `requirements.txt`；Windows 和 macOS 通用 Python 代码 |
| iPhone 视频预览 | 另装 FFmpeg 可执行程序并加入 PATH；安装 PyAV 不会替代它 |
| 完整三路硬件 GUI 采集 | 当前 D435 一键控制仍依赖 macOS shell/sudo；Windows 会显示明确的暂不支持提示 |
| iPhone App 编译部署及 USB 接收 | 编译部署需要 macOS + 完整 Xcode；当前 USB 接收器使用 Unix usbmuxd 套接字，尚未完成 Windows USB 链路移植。TCP 接收需另行配置并实测 |
| 模块 01 的独立 RealSense Python 采集 | 需匹配系统和 Python 的 librealsense/pyrealsense2 SDK；Windows 按 Intel SDK 安装指导配置，macOS 见模块 01 的安装脚本 |
| 模块 03 旧 CSV、绘图工具 | `python -m pip install -r requirements/coinft-tools.txt`；Tk 绘图还需 Python 的 tkinter 组件 |
| 模块 03 标定和训练工具 | `python -m pip install -r requirements/calibration.txt`；NI 硬件还需厂商 NI-DAQmx 驱动，macOS 不支持完整 NI 采集链路 |
| 测试 | `python -m pip install -r requirements/dev.txt` |
| Teensy 固件编译烧录 | PlatformIO / Teensy 工具链，见模块 03；Python 环境安装不包含固件工具链 |

旧标定脚本 `wrench_calibration.py` 还引用未随包交付的 `PyriteUtility.computer_vision.imagecodecs_numcodecs`。运行该旧工具前需要提供原 PyriteUtility 工程；基础环境不包含它。训练清单使用兼容 Intel Mac 的 PyTorch 2.2.2 CPU 默认安装；CUDA/GPU 环境需按 PyTorch 官方平台选择单独配置。可选硬件和旧训练环境不属于网页启动所需依赖。

依赖版本固定是为了避免 NumPy 2、Zarr 3 和旧 API 不兼容。OpenCV 统一安装 **opencv-contrib-python**（包含 ArUco），不要同时安装其他提供 `cv2` 的包。这些清单固定直接依赖，传递依赖仍由 pip 解析，并非所有平台的完整 lockfile。

## 常见报错

- `ModuleNotFoundError`：先检查正在使用的解释器，始终用同一个 `python -m pip` 安装，再用它启动。Windows 直接用上面的 `.venv` 完整命令。
- `fcntl` 或 `fchmod` 报错：本版已使用跨平台文件锁和原子写入；若仍出现，确认运行的是更新后的交付包，不是另一个旧目录。
- Zarr / NumPy API 或二进制兼容错误：创建新的 `.venv` 并安装根目录清单，不要继续向旧的复杂 Conda 环境叠加包。
- `cv2.aruco` 缺失：卸载所有 OpenCV 变体后，用 `python -m pip install --force-reinstall -r requirements.txt` 重装。
- FFmpeg 找不到：确认 `ffmpeg -version` 成功，安装后重开终端。环境检查仅检查可执行文件可发现性，实际视频仍需支持对应编解码器。
- D435 在 Windows 无法 Arm：现有网页 D435 控制还未移植；安装 pyrealsense2 不会使 macOS 控制脚本变成 Windows 程序。
- 缺 DLL / ONNX Runtime 无法导入：Windows 使用 64 位 Python，并安装 Microsoft Visual C++ 2015–2022 x64 Redistributable，再运行环境检查。

## 验证

环境检查不连接硬件，也不写入正式数据。软件测试从项目根目录执行：

```bash
python -m pip install -r requirements/dev.txt
python -m pytest modules/04_laptop_alignment_export/tests
```

原生 Windows 的文件锁、网页 HTTP 和数据导出回归由 `.github/workflows/python-portability.yml` 运行。本机 macOS 验证不能替代 Windows 实机和三路硬件验收。
