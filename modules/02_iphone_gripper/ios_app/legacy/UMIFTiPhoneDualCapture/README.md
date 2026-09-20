# UMIFTiPhoneDualCapture

当前定位：这是最终 iPhone 采集 App 的改造目标目录。

## 当前主线

`UMIFTiPhoneDualCapture` 原本是 `AVCaptureMultiCamSession` 的双摄 scaffold。现在这条路线已经被实机验证结果替换：

```text
ARKit WorldTracking is the only camera owner
  -> wide RGB / depth / 6DoF pose / intrinsics
  -> private ARFrame ultrawide side stream for gripper marker vision
```

也就是说，后续不再让 `AVCaptureMultiCamSession` 和 `ARKit` 抢相机。App 启动后应长期运行一个 `ARSession`，wide 主流和 ultrawide 低频副流都从 `ARFrame` 出来。

## 改造方案

详细迁移计划见：

- [docs/arkit_private_ultrawide_migration_plan.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/ios_app/UMIFTiPhoneDualCapture/docs/arkit_private_ultrawide_migration_plan.md)

这份方案固定了：

- 哪些旧模块保留思想。
- 哪些旧模块删除或降级。
- 新的 ARKit 主 owner 架构。
- 私有 ultrawide 副流的 gripper 检测链路。
- 校准流程、UI、缓冲、时间戳对齐和传输里程碑。

## 当前旧代码状态

当前目录里仍然存在旧 scaffold：

- `DualCameraCaptureManager.swift`：旧 `AVCaptureMultiCamSession` 主链路，后续应由 `ARCaptureSessionController` 替代。
- `DualCameraPreviewView.swift`：旧 `AVCaptureVideoPreviewLayer` 预览，后续应替换为 ARKit wide 预览 + private ultrawide 640x480 预览。
- `WorldCalibrationARView.swift`：可保留并提升为常驻 ARKit 视图，而不是只在世界校准阶段临时启动。
- `GripperVisionProcessor.swift`：只保留一维路径标定和 overlay 思想；输入、ROI、候选筛选、OpenCV bridge 要按 640x480 private ultrawide 副流重写。

## 验证来源

窄验证 App：

```text
ios_app/UMIFTiPhoneARUltraWidePoseCheck/
```

已经在真机证明：

```text
ARWorldTracking wide主流: 1920x1440, about 60fps
private ultrawide副流: 640x480, 420f, about 10fps
ARKit pose: tracking normal
```

后续迁移时只把验证 app 中已经证实的能力搬入 `UMIFTiPhoneDualCapture`，不继续在验证 app 里堆最终采集功能。

## 构建命令

```sh
DEVELOPER_DIR=/Users/550m/Downloads/Xcode.app/Contents/Developer \
xcodebuild \
  -project UMIFTiPhoneDualCapture.xcodeproj \
  -scheme UMIFTiPhoneDualCapture \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO \
  build
```
