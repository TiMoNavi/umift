# 模块 02：iPhone ARKit 采集与夹爪开合度

更新时间：2026-06-29

## 一句话目标

把固定在夹爪上的 iPhone 变成一个同步采集节点：

```text
ARKit wide RGB + depth + 6DoF pose + camera parameters
ARKit private ultrawide side stream -> gripper marker -> gripper_open_percent
统一时间戳 -> 手机内存 3 秒环形缓冲 -> 发送到电脑
```

屏幕只是监看和控制界面。采集主线只有一个相机 owner：`ARSession`。

## 已验证的关键事实

真机验证已经确认：

```text
ARWorldTrackingConfiguration
  public main stream:
    ARFrame.capturedImage = wide RGB, 1920x1440, about 60fps
    ARFrame.camera.transform = 6DoF pose
    ARFrame.camera.intrinsics = wide intrinsics

  private side stream:
    ARFrame.capturedUltraWideImage = ultrawide image, 640x480, 420f, about 10fps
    ARFrame.ultraWideImageTimestamp = ultrawide timestamp
    ARFrame.ultraWideCamera = ultrawide intrinsics
```

因此新的主线是：

```text
ARKit world tracking
  -> wide 主摄画面、depth、pose、内参
  -> private ultrawide 低频副流
  -> 夹爪 marker 检测与开合度估计
```

这条路线替代此前的：

```text
AVCaptureMultiCamSession wide + ultra
ARKit + independent AVCapture ultra
wide-only marker fallback
ARPositionalTracking ultrawide primary pose
```

## 重要边界

`capturedUltraWideImage`、`ultraWideImageTimestamp`、`ultraWideCamera` 是私有 ARKit runtime 成员，不是公开 App Store API。

当前项目是内部采集工具，可以使用这条路线，但必须：

- 启动时做 runtime capability probe。
- UI 明确显示 private ultrawide 是否可用。
- 日志记录 iOS 版本、设备型号、ARKit video format、selector 是否存在。
- 保留降级/报错路径，不假装 ultrawide 一定存在。

## 目录定位

```text
02_iphone_gripper/
├── 01_app_build_and_deploy/       # 构建、安装、真机调试
├── 02_apple_capture_flow/         # Apple 采集流程记录
├── 03_gripper_marker_vision/      # 夹爪 marker 方案与离线工具
├── ios_app/
│   ├── UMIFTiPhoneCaptureCore/           # 当前真正生效的 iPhone 采集 app
│   ├── legacy/                           # 旧原型、验证 app、已过时方向
│   └── vendor/                           # iOS app 共享第三方依赖
└── tools/                         # Mac 端辅助脚本
```

当前 app 目标目录：

```text
ios_app/UMIFTiPhoneCaptureCore/
```

构建入口：

- [ios_app/UMIFTiPhoneCaptureCore/README.md](ios_app/UMIFTiPhoneCaptureCore/README.md)
- iPhone 到 Mac 实时主帧链路重写设计属于外部设计记录，未随本交付包提供。

历史原型和验证 app 已归档到：

- [ios_app/legacy/README.md](ios_app/legacy/README.md)

## 模块职责

### ARCapture

职责：

- 维护唯一长期运行的 `ARSession`。
- 输出 wide RGB、pose、intrinsics、depth。
- 通过 runtime private getter 输出 ultrawide 640x480 副流。
- 做 timestamp 统一和能力探测。

不允许：

- 启动第二个 `AVCaptureSession`。
- 在 calibration/preview/recording 切换时反复释放再抢占相机。

### GripperVision

职责：

- 输入 private ultrawide `CVPixelBuffer`。
- 使用 OpenCV 或后续自定义检测器识别左右 marker。
- 输出左右 marker 角点、中心点、面积、状态和置信度。
- 基于一维路径标定输出 `gripper_open_percent`。

设计原则：

- 左 marker：`id 0`。
- 右 marker：`id 1`。
- 夹爪运动建模为图像上的一维路径。
- 0% 和 100% 标定一次，后续用路径位置恢复开合度。
- 快速运动允许短暂 hold，但不能允许候选满屏乱飞。
- 旧 AVCapture ultrawide 的 ROI/候选阈值不能直接复用。

### Calibration

包含两个子流程：

```text
Gripper path calibration
World origin calibration
```

夹爪路径标定：

- 显示 private ultrawide 640x480 画面。
- 用户开合夹爪 3 次。
- 采集左右 marker 轨迹。
- 估计 closed/open/path。
- 保存 `GripperCalibration`。

世界坐标标定：

- 显示 ARKit wide 主画面。
- 屏幕中心对准桌面原点。
- 用户点击或夹爪闭合一次触发标记。
- 用中心 raycast / depth 设置世界原点。
- 用重力和相机视线水平投影定义世界方向。
- 显示固定在现实桌面上的 XYZ 坐标轴，观察 10 秒。
- 保存 `WorldCalibration`。

### Buffering / Sync

职责：

- 手机内维护约 3 秒环形缓冲。
- wide 主帧作为主时间轴。
- 对齐最近的 gripper sample。
- 记录 sample age 和 sync error。
- 录制阶段持续产生 `AlignedCaptureState`。

注意：ultrawide 副流约 10fps，不能再用 20ms 这种同频同步阈值。必须明确区分：

```text
strict sync
nearest sample
hold state
stale / invalid
```

### Transport

职责：

- 把对齐后的状态持续发送到电脑。
- receiver 断开时 UI 必须明确显示。
- 第一版不在 iPhone 端提前裁剪成最终下游数据契约。

原则：

```text
Apple side captures it -> attach timestamp -> send it
```

## App 状态机

顶层状态：

```text
PreviewUncalibrated
CalibrationGripper
CalibrationWorld
PreviewCalibrated
StartCountdown
Recording
```

相机生命周期：

```text
App launch -> ARKit start once -> app quit
```

状态变化只改变 UI、校准采样、录制写入和发送逻辑，不再 stop/start 相机 session。

## UI 固定

横屏布局：

```text
┌──────────────────────────────┬──────────────────────────────┐
│ left: 4:3 preview + controls │ right: parameter panel        │
│ Wide / Ultra switch          │ device / ar / gripper / sync │
│ Record / Calibrate / Torch   │ calibration / event log       │
└──────────────────────────────┴──────────────────────────────┘
```

左侧预览：

- `Wide`：ARKit 主画面、世界坐标轴、wide/depth overlay。
- `Ultra`：private ultrawide 640x480 画面、marker 框、候选框、ROI、路径。

右侧参数面板必须显示：

- ARKit running/tracking state。
- private ultrawide available/unavailable。
- wide frame resolution/fps。
- ultrawide frame resolution/fps。
- 6DoF pose。
- center depth。
- gripper open percent。
- gripper confidence。
- left/right marker state。
- sync age/error。
- buffer duration。
- receiver connected/disconnected。

## 数据结构草案

### GripperState

```text
GripperState {
  confidence
  open_percent
  left_marker_state
  right_marker_state
  gripper_distance_m?
}
```

### MarkerState

```text
MarkerState {
  marker_id
  detected
  tracking_state       # decoded / candidate / held / lost / rejected
  confidence
  center_px
  corners_px
  area_px2
  path_position?
  path_error_px?
  last_seen_timestamp_ns?
}
```

### AlignedCaptureState

```text
AlignedCaptureState {
  timestamp_ns
  wide_rgb
  wide_intrinsics
  camera_pose_world
  depth
  gripper
  sync
  events
}
```

## 实现里程碑

### Milestone 0：冻结旧 MultiCam 主线

- 不再继续在 `AVCaptureMultiCamSession` 上叠功能。
- 新实现只以 ARKit 为相机 owner。
- 文档和 UI 明确当前主线。

### Milestone 1：迁入 ARKit + private ultrawide 验证能力

- App 启动运行 ARKit world tracking。
- 显示 wide ARKit 预览。
- 显示 private ultrawide 640x480 预览。
- 右侧显示 private ultrawide availability。

### Milestone 2：接入新版 ultrawide gripper detection

- 从 private ultrawide `CVPixelBuffer` 走 OpenCV。
- 返回 id0/id1 角点和候选四边形。
- 只在 `Ultra` 预览上显示 marker overlay。

### Milestone 3：夹爪一维路径标定

- 开合 3 次采样。
- 保存左右 marker 一维路径。
- 输出 `open_percent` 0-100。
- 支持 hold、单 marker 降级、路径误差置信度。

### Milestone 4：世界坐标校准常驻化

- ARKit 不因进入/退出世界校准而重启。
- 保留中心 raycast 标记原点。
- 输出 `user_world_from_arkit`。

### Milestone 5：3 秒环形缓冲和时间戳对齐

- 建立 wide/pose/depth/gripper buffers。
- wide 主时间轴生成 aligned state。
- UI 显示 buffer duration、gripper sample age、sync error。

### Milestone 6：录制状态机

- 已校准后允许点击录制。
- 夹爪闭合 3 秒触发倒计时。
- 倒计时开始后不因触发条件消失取消。
- 终止方式只保留屏幕终止按钮。

### Milestone 7：电脑端传输

- 从 aligned ring buffer 持续发送。
- 不在 iPhone 端提前裁剪字段。
- receiver 断开时 UI 明确显示。

## 近期执行顺序

1. 继续维护 `UMIFTiPhoneCaptureCore` 作为唯一 iPhone app 主线。
2. 保持 `ARKit WorldTracking` 为唯一长期相机 owner。
3. 收敛 gripper marker 检测、路径标定、world calibration、buffer、recording、transport。
4. Mac 端通过模块 04 的 Protocol V2 receiver 接收 `17381` 端口 H.264/depth/metadata binary frame bundle。
5. 旧 app 只从 `ios_app/legacy/` 查阅，不再作为新增功能目标。
