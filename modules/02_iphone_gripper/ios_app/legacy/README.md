# Legacy iPhone Apps

这些目录是历史原型、验证 app 或已放弃的方向，保留用于追溯和参考，不再作为当前采集主线。

当前真正生效的 iPhone app 是：

```text
../UMIFTiPhoneCaptureCore/
```

归档内容：

- `UMIFTiPhoneCaptureMVP/`：早期 ARKit 导出结构和 demo 拉取 MVP。
- `UMIFTiPhoneDualCapture/`：原 `AVCaptureMultiCamSession` 双摄 scaffold，后来作为迁移原型。
- `UMIFTiPhoneARUltraWidePoseCheck/`：private ultrawide ARFrame 能力的窄验证 app。
- `UMIFTiPhoneARWideGripper/`：wide marker 方向的规划/实验目录，没有作为实际 Xcode app 主线。

注意：

- 这里面的 README、handoff、脚本命令可能仍保留当时的旧路径和旧 bundle id。
- 新开发、构建、真机安装、Mac 端接收都应使用 `UMIFTiPhoneCaptureCore`。
