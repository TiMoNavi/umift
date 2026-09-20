# reference 目录说明

这个目录放的是“参考资料”和“外部来源整理”，不是当前项目的主规范。

它的角色更像：

```text
我需要知道某个硬件/方案原本怎么做
-> 来 reference 查
```

而不是：

```text
我现在应该怎么实现本项目
-> 直接看 reference
```

## 这里面有什么

- `intel_realsense_d435/`
  - D435 / D435i 参考操作
- `recording_trigger_and_coordinates/`
  - 录制触发和坐标系约定来源
- `gripper_marker_width/`
  - 视觉夹爪宽度方案参考
- `teensy41_coinft_bridge/`
  - Teensy 串口桥接资料
- `coinft_calibration_firmware/`
  - CoinFT 固件 / 标定资料

## 什么时候看这个目录

- 你要找外部资料来源
- 你要核对 UMI / UMI-FT 的历史实现
- 你要补固件、桥接、标定背景

## 什么时候别先看这里

如果你只是想知道“当前项目到底要求我输出什么”，先看：

- `modules/`
- `docs/`
