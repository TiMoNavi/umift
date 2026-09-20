# UMIFT iPhone AR UltraWide Pose Check

状态：窄验证 App，不作为最终采集 App。

## 已验证结论

这个 App 原本只用于验证：

```text
ARKit 是否能用 ultrawide 作为主摄跑 6DoF
```

实机验证后，真正有价值的结论变成：

```text
ARWorldTracking 使用 wide 主流正常跑 6DoF
同时 ARFrame runtime 内存在 private ultrawide side stream
```

真机观测：

```text
ARWorldTracking public stream:
  BuiltInWideAngleCamera 1920x1440 about 60fps

private ARFrame side stream:
  capturedUltraWideImage 640x480
  pixelFormat 420f
  about 10fps
  ultraWideImageTimestamp available
  ultraWideCamera intrinsics available
```

## 当前用途

这个 App 只用于验证和抽取能力：

- ARKit world tracking 是否稳定。
- private ultrawide selector 是否存在。
- ultrawide 副流是否能显示。
- 640x480 副流的 OpenCV gripper 检测入口是否能跑。

最终功能不继续堆在这个 App 里。已经验证通过的代码应迁移到：

```text
../UMIFTiPhoneDualCapture/
```

迁移方案：

- [../UMIFTiPhoneDualCapture/docs/arkit_private_ultrawide_migration_plan.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/ios_app/UMIFTiPhoneDualCapture/docs/arkit_private_ultrawide_migration_plan.md)

## 注意

`capturedUltraWideImage`、`ultraWideImageTimestamp`、`ultraWideCamera` 是私有 runtime 成员。

该能力适合内部采集工具验证，不适合 App Store 发布。迁入主 App 时必须保留 runtime capability probe 和不可用状态显示。
