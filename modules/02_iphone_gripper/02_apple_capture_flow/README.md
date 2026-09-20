# Part 02：Apple 采集流程

这一部分只讨论：

- iPhone 端采什么
- 6DoF 原点怎么校准
- 录制界面和交互怎么工作
- 导出的 demo 数据长什么样

## 推荐先看

- 当前采集流程以本 README 和 `ios_app/UMIFTiPhoneCaptureCore/README.md` 为准；独立 `capture_design.md` 未随本包提供。
- [ios_app/UMIFTiPhoneCaptureCore/README.md](../ios_app/UMIFTiPhoneCaptureCore/README.md)
- [App/ARCapture/ARCaptureModel.swift](../ios_app/UMIFTiPhoneCaptureCore/App/ARCapture/ARCaptureModel.swift)
- [App/UI/CaptureDashboardView.swift](../ios_app/UMIFTiPhoneCaptureCore/App/UI/CaptureDashboardView.swift)

## 当前流程摘要

1. 进入 App
2. 先进入 `Calibration` 阶段
3. 手机横屏平放
4. 用重力锁定竖直轴
5. 用手机长边方向建立水平轴，并吸附到最近的 90 度主方向
6. 点击 `Calibrate`
7. 进入 `Ready / Record`
8. 开始采集：
   - RGB
   - Depth
   - 6DoF pose

## 当前 UI 摘要

横屏采集 UI 现在已经改成：

- 透明边缘 HUD
- 放大录制按钮
- 详情折叠面板

目标就是：

- 不挡住主画面
- 还能看关键状态
