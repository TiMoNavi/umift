# CoinFT 固件与标定文件来源

CoinFT 相关资料理论上来自：

```text
/Users/550m/code/umift_recovered_reference
```

## 已找到的固件

CoinFT sensor board 固件镜像：

```text
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/hardware_files/CoinFT_V2_firmware.hex
```

注意：

- 这是 CoinFT 传感器板 MCU 的 `.hex`，不是 Teensy 固件。
- 目前没有完整源代码。
- 不建议第一版重刷 CoinFT sensor board，除非确认板子没有固件或固件损坏。

## 已找到的 Teensy bridge 源码

```text
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/teensy_coinft_serial_interface.cpp
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/platformio.ini
```

这部分用于刷 Teensy 4.1。

## 已找到的标定模型

本地可见 ONNX：

```text
/Users/550m/code/umift_recovered_reference/external_sources/hardware_interfaces/hardware/coinft/config/UFT3_MLP_4L_scl_1_30.onnx
/Users/550m/code/umift_recovered_reference/external_sources/hardware_interfaces/hardware/coinft/config/UFT4_MLP_4L_scl_1_30.onnx
```

配置文件中还提到过这些模型/归一化文件：

```text
FFT0_MLP_5L_norm_L2.onnx
FFT1_MLP_5L_norm_L2.onnx
FFT0_norm.json
FFT1_norm.json
SFT9-3_MLP_5L_norm_L2.onnx
SFT14-2_MLP_5L_norm_L2.onnx
SFT9-3_norm.json
SFT14-2_norm.json
SFT2-6_MLP_5L_norm_L2.onnx
UFT10-4_MLP_5L_norm_L2.onnx
SFT2-6_norm.json
UFT10-4_norm.json
```

但不是所有文件都已在本机找到。下一步要确认你的两个 CoinFT 对应哪一组标定文件。

## UMI-FT 标定计算路径

原版/恢复代码显示：

```text
raw 12 channel
-> tare offset
-> normalize with norm json
-> ONNX inference
-> denormalize
-> 6D wrench [Fx,Fy,Fz,Mx,My,Mz]
```

笔记本端应优先复用这个逻辑，而不是把 ONNX 塞进 Teensy 或 iPhone。

## 需要建立的本项目目录

建议后续把实际使用文件复制到：

```text
reference/coinft_calibration_firmware/assets/
├── coinft_sensor_firmware/
├── teensy_bridge/
└── calibration/
    ├── left.onnx
    ├── right.onnx
    ├── left_norm.json
    └── right_norm.json
```

当前先只记录来源，不自动复制，避免误用不匹配的模型。
