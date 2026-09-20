# 模块 03 资产目录

这个目录预留给真正会被本项目使用的 CoinFT / Teensy 资产。

当前只收纳会被本项目直接使用的资产。旧的 demo/runtime bundle 和固定两路 UART 说明不再放在这里，避免和当前自动检测固件混淆。

当前已经确认：

1. Teensy 4.1 bridge 固件位于 `steps/04_teensy_bridge_firmware/firmware/`
2. 两只当前生产 CoinFT 标定模型位于 `assets/calibration/production/`，映射为 `0019 -> left`、`0012 -> right`
3. left/right 按实际 active Teensy Serial/pins 编号高低决定

子目录用途：

```text
assets/
├── coinft_sensor_firmware/   # CoinFT 板载固件，例如 .hex
└── calibration/              # 生产 ONNX / norm.json / manifest
```

`coinft_2606602_0008` 是历史标定资产，保留用于追溯，不属于当前默认运行组合。

建议原则：

- 只有确认要用于本项目的资产，才复制到这里
- 一旦复制进来，就在旁边补来源说明和版本说明
