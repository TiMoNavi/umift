# CoinFT / Teensy 固件与资产清单

本文档只记录当前项目应该使用的 CoinFT / Teensy 资产。早期固定串口、旧 board 配置、旧 demo/runtime bundle 和不匹配模型名已经从本模块清理掉。

## 1. CoinFT Sensor Board 固件

当前保留资产：

```text
steps/01_coinft_sensor_firmware_flash/assets/CoinFT_V2_firmware.hex
steps/01_coinft_sensor_firmware_flash/assets/CFT_V2_BOM_PCBA.xlsx
steps/01_coinft_sensor_firmware_flash/assets/CFT_V2_CPL_revised.xlsx
```

当前判断：

- `.hex` 是 CoinFT sensor board 本体固件，不是 Teensy bridge 固件
- 当前没有 CoinFT sensor board 的完整源码
- 不建议默认重刷 CoinFT 本体；只有确认板子空白或固件损坏时才进入 Step 01

## 2. Teensy Bridge 固件

当前使用工程：

```text
steps/04_teensy_bridge_firmware/firmware/
```

当前行为：

- 目标板：`Teensy 4.1`
- CoinFT UART：所有 Teensy 4.1 hardware UART candidates 以 `1000000` baud 打开
- Host USB serial：按 `115200` 打开
- 运行时检测收到合法 CoinFT frame 的 active UART
- 两块 CoinFT 在线时，编号更高的 active Serial/pins 输出为 `left`，编号更低的输出为 `right`
- 双路包是 58 bytes：`0x00 0x00 + sequence_id + teensy_time_us + left raw 12ch + right raw 12ch`
- 没有真实 CoinFT 在线时可输出 58-byte 模拟双路包，方便先打通 host 链路

当前 mapping：

```text
left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
right = lower numbered active Teensy Serial/pins  = coinft_2606602_0012
```

权威记录：

```text
assets/calibration/production/current_left_right_mapping.json
```

## 3. Production 标定模型

当前 runtime 直接使用：

```text
assets/calibration/production/coinft_2606602_0019/
assets/calibration/production/coinft_2606602_0012/
```

每个目录里 runtime 关键文件：

```text
*_MLP.onnx
CFT24_MLP.onnx.data
*_norm.json
manifest.json
```

说明：

- `CFT24_MLP.onnx.data` 只是 ONNX 外部权重文件名兼容需求，不表示当前模型来自旧 demo
- `*_MLP.pth`、`*_MLP_results.png`、`source_README.md` 只用于审计和追溯，不是常规 runtime 必需
- 原始 H5 和中间训练文件没有搬入本模块，仍留在 Downloads 源目录

## 4. 已清理的旧内容

旧内容不再作为当前项目资产保留。清理原因：

- 固定某两个 UART 的说明容易和当前自动检测固件混淆
- 旧 board 配置不是当前 Teensy 4.1 工程
- 旧 host 协议不包含当前 `sequence_id` 和 `teensy_time_us`
- 旧 demo 模型名不是当前两个真实 CoinFT 的 production 模型

## 5. 当前优先验证项

1. 编译并上传 Step 04 Teensy 4.1 固件
2. 用 smoke test 确认 58-byte 包和 `sequence_id/teensy_time_us`
3. 接入两块真实 CoinFT 后跑 `d` 诊断命令
4. 确认诊断输出中的高编号 active Serial/pins 对应左侧 0019，低编号对应右侧 0012
5. Host 侧使用 production ONNX/norm 路径做 raw -> wrench 转换
