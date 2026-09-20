# Intel RealSense D435 / D435i 操作参考

外置场景全局摄像头型号按 `D435` 系列处理。你手里当前设备是 **Intel RealSense D435i**。

## 角色

D435 / D435i 负责全局视角：

```text
D435 RGB/depth -> USB -> Mac/Windows laptop -> global_camera/
```

它不替代 iPhone 夹爪视角。D435 用于观察场景整体、任务状态、接触事件和后续多视角训练数据。

## 推荐软件栈

第一版推荐使用 Intel RealSense SDK 2.0 / librealsense：

- `realsense-viewer`：检查设备、曝光、深度是否正常。
- `pyrealsense2`：Python 采集 RGB/depth 和 frame timestamp。
- `rs-enumerate-devices`：列出设备和序列号。

官方入口：

- Intel RealSense SDK 2.0: `https://github.com/IntelRealSense/librealsense`
- D435 产品页: `https://www.intelrealsense.com/depth-camera-d435/`

## Mac / Windows 取舍

Windows 通常是 D435 最省心的平台。Mac 可以尝试 librealsense，但老 Mac、系统版本、USB 控制器和权限都可能影响稳定性。

本项目第一版策略：

```text
Mac 优先负责 iPhone 拉取和总体开发
Windows 可作为 D435/CoinFT 采集备选
```

如果 Mac 上 D435i 不稳定，不要在这里消耗过多时间，可以改成：

```text
Windows laptop captures D435 + CoinFT
Mac pulls iPhone
两台电脑后处理合并
```

但当前主设计仍是单笔记本 coordinator。

## 采集内容

每帧保存：

```text
frame_index
host_receive_time_s
realsense_frame_timestamp_ms
realsense_timestamp_domain
rgb_frame_path or rgb video index
depth_frame_path or depth array index
```

第一版不采 IMU，因此 D435i 的额外 IMU 能力先忽略。

输出建议：

```text
global_camera/
├── rgb.mp4
├── depth.zarr or depth.raw
├── frame_timestamps.csv
└── camera_metadata.json
```

## 最小操作步骤

1. 插上 D435。
2. 打开 `realsense-viewer`。
3. 确认 RGB 和 depth 都有画面。
4. 记录序列号、分辨率、FPS。
5. 用采集脚本保存 10 秒测试数据。
6. 检查 RGB frame 数、depth frame 数和 timestamp 是否连续。

## 第一版建议参数

先不要追高规格，先追稳定：

```text
RGB: 640x480 or 848x480, 30 FPS
Depth: 640x480 or 848x480, 30 FPS
alignment: depth aligned to color if SDK stable
```

后续如果训练需要更高分辨率，再升级。

## 与 iPhone 坐标系的关系

D435 的坐标系和 iPhone ARKit 世界坐标系不是同一个东西。第一版只做时间轴对齐，不做空间外参融合。

如果训练格式需要同一世界坐标系，需要后续增加：

- D435 内参。
- iPhone 相机内参。
- D435 到 iPhone/任务世界的外参标定。
- 可见 marker / calibration board / hand-eye calibration。
