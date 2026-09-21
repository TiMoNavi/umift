# 模块 03：CoinFT + Teensy

新电脑先按 [Windows / macOS 环境安装](../../docs/INSTALL.md) 创建虚拟环境并安装根目录 `requirements.txt`。可选的旧标定/训练依赖单独列出；D435 GUI 控制和 iPhone USB 接收目前仍有平台限制，见安装指南。

这个目录现在按真实开工顺序重排，不再只是“资料堆放区”。

如果你要在新窗口继续接手，先看：

- [docs/firmware_inventory.md](docs/firmware_inventory.md)
- [docs/coinft_to_module04_integration.md](docs/coinft_to_module04_integration.md)

目标很简单：

```text
从 CoinFT 板子本体
-> 到 CoinFT 校准
-> 到 Teensy 基本信息
-> 到 Teensy bridge 固件
-> 到第一次把 CoinFT 数据抓出来
-> 到 Mac / UMI-FT host 侧驱动
```

每一步都尽量做到：

1. 有自己的 README
2. 有对应代码
3. 有对应资产
4. 有上游来源说明

## 推荐阅读顺序

### Step 01. CoinFT 固件烧录

路径：

- [steps/01_coinft_sensor_firmware_flash](steps/01_coinft_sensor_firmware_flash)

处理什么：

- CoinFT sensor board 本体固件 `.hex`
- BOM / CPL
- 板子 MCU 与烧录不确定项

### Step 02. CoinFT 校准

路径：

- [steps/02_coinft_calibration](steps/02_coinft_calibration)

处理什么：

- 新做出来的 CoinFT 如何和参考六维力传感器一起采样
- 当前已经确认的一条替代路线是 `DEDH-75D-50NX6`
- 如何得到 `norm.json`
- 如何训练 `ONNX`
- 当前 production 模型资产：`coinft_2606602_0019` / `coinft_2606602_0012`

补充参考资料：

- [supporting_tools/dedh_reference_sensor](supporting_tools/dedh_reference_sensor)

### Step 03. Teensy 基本信息

路径：

- [steps/03_teensy_basics](steps/03_teensy_basics)

处理什么：

- Teensy 4.1 在这条链路里的角色
- 串口分工
- 接线原则
- USB / UART 基础验证

### Step 04. Teensy 固件与转发程序

路径：

- [steps/04_teensy_bridge_firmware](steps/04_teensy_bridge_firmware)

处理什么：

- 当前 Teensy 4.1 bridge 固件源码
- `PlatformIO` 配置
- `Teensy -> Mac` 的 58-byte 转发协议
- smoke test

### Step 05. 第一次抓 CoinFT

路径：

- [steps/05_first_coinft_capture](steps/05_first_coinft_capture)

处理什么：

- 先不追完整系统
- 先把 CoinFT 原始流从 Teensy 抓到 Mac
- 跑 smoke test 和离线 UART 采集脚本

### Step 06. Host 侧驱动与转发

路径：

- [steps/06_host_side_driver_and_forwarding](steps/06_host_side_driver_and_forwarding)

处理什么：

- UMI-FT / hardware_interfaces 那条 C++ host 驱动
- `CoinFTBus`
- `manip_server`
- YAML 配置

## 你现在最该看哪一步

### 如果你手里还没有接实物

先看：

1. Step 02 校准
2. Step 03 Teensy 基本信息
3. Step 04 Teensy bridge 固件

### 如果你已经拿到 Teensy 4.1，但还没出数据

先看：

1. Step 03
2. Step 04
3. Step 05

### 如果你已经能出 raw 数据，想接回 UMI-FT/UMIFT

先看：

1. Step 02
2. Step 05
3. Step 06
4. [docs/coinft_to_module04_integration.md](docs/coinft_to_module04_integration.md)

04 主链路当前推荐配置：

- [configs/coinft_04_laptop_calibrated.json](configs/coinft_04_laptop_calibrated.json)

## 当前仍然缺什么

- CoinFT sensor board 固件源码
- 真实板子的电源/线序/烧录针脚现场确认
- 真机接线后的左右映射复核
- 主系统里最终使用的串口路径

但和之前不同的是：

```text
两个当前 CoinFT 的 production ONNX/norm 已经归位
left/right 映射已经写入 assets/calibration/production/current_left_right_mapping.json
04 主链路 calibrated config 已经写入 configs/coinft_04_laptop_calibrated.json
```
