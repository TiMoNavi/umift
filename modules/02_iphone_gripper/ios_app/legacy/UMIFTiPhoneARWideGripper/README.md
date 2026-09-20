# UMIFT iPhone AR Wide Gripper

这是新的 iPhone 夹爪采集 App 方向，目标是放弃 `ultra-wide + independent AVCaptureSession`，改为单一 `ARKit world tracking + wide camera` 主链路。

旧版 `UMIFTiPhoneDualCapture` 和 `UMIFTiPhoneCaptureMVP` 只作为经验参考：

- 保留：marker 路径坐标法、开合度 0-100、三次开合标定、状态保持、候选可视化、正式录制前必须校准。
- 不保留：ultra-wide 相机参数、AVCapture 独立取流、旧 ROI、旧畸变筛选阈值、旧 UI 状态机的相机切换假设。

新 App 的核心假设：

1. ARKit 必须稳定运行在后置 wide 相机上，用于 6DoF、RGB、depth、时间戳。
2. 夹爪 marker 必须通过机械/贴装调整进入 wide 可见区域。
3. 夹爪开合检测直接处理 `ARFrame.capturedImage`，不再启动第二个相机 session。
4. 所有标定、识别阈值、ROI、路径坐标都重新建立，不能沿用 ultra-wide 版本参数。

详细规划见：

- [docs/arkit_wide_marker_plan.md](docs/arkit_wide_marker_plan.md)

