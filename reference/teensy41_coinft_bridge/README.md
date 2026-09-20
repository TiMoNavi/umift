# Teensy 4.1 刷写、连接和使用

当前硬件：**Teensy 4.1**。它现在没有应用固件，需要先刷入 CoinFT bridge 固件。

## 目标链路

```text
CoinFT left  -> Teensy Serial2
CoinFT right -> Teensy Serial3
Teensy USB   -> Mac/Windows laptop
```

Teensy 只做低层桥接：

```text
CoinFT UART frames -> 50-byte USB serial packets
```

ONNX/norm 标定和 6D wrench 计算放在笔记本端。

## 来源文件

本地恢复资料：

```text
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/teensy_coinft_serial_interface.cpp
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/platformio.ini
/Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/coinft_teensy_smoke_test.py
```

官方 `platformio.ini` 目标是 Teensy 4.0：

```ini
[env:teensy40]
platform = teensy
board = teensy40
framework = arduino
upload_protocol = teensy-cli
```

你手里是 Teensy 4.1，因此第一版需要复制一份并改成：

```ini
[env:teensy41]
platform = teensy
board = teensy41
framework = arduino
upload_protocol = teensy-cli
```

## 安装工具

推荐 PlatformIO：

```bash
python3 -m pip install platformio
```

也可以用 VS Code PlatformIO 插件。

## 刷写步骤

建议不要直接改 recovered reference，复制到本项目后再改：

```bash
mkdir -p /Users/550m/code/UMIFT-datacollect/reference/teensy41_coinft_bridge/firmware
cp /Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/teensy_coinft_serial_interface.cpp \
  /Users/550m/code/UMIFT-datacollect/reference/teensy41_coinft_bridge/firmware/
cp /Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/Teensy/platformio.ini \
  /Users/550m/code/UMIFT-datacollect/reference/teensy41_coinft_bridge/firmware/
```

把 `platformio.ini` 的 `board = teensy40` 改为 `board = teensy41`。

然后：

```bash
cd /Users/550m/code/UMIFT-datacollect/reference/teensy41_coinft_bridge/firmware
pio run
pio run -t upload
```

如果上传等待设备，按 Teensy 4.1 的 program button。

## Mac 上验证

刷写成功后，Mac 应该出现串口：

```bash
ls /dev/cu.usbmodem*
```

如果仍然没有串口，只看到 `16c0:0478` 这类 bootloader/composite device，说明应用固件没有正常运行或还在 bootloader 状态。

## 串口协议

```text
host -> Teensy:
i = idle
s = start streaming
t = tare

Teensy -> host:
0x00 0x00 + left 12 x uint16 little-endian + right 12 x uint16 little-endian
total: 50 bytes
baud: 115200
```

## CoinFT 接线

官方代码使用：

```text
CFT1 = Serial2
CFT2 = Serial3
CoinFT UART baud = 1000000
USB Serial baud = 115200
```

Teensy 4.x 常用 UART 引脚需要按 PJRC pinout 最终确认。接线原则：

```text
Teensy RX <- CoinFT TX
Teensy TX -> CoinFT RX
GND       -> common GND
logic     -> verify 3.3V TTL before powering
```

不要在没确认电平前直接接 5V TTL。

## Smoke Test

刷好 Teensy 并连接 CoinFT 后：

```bash
python /Users/550m/code/umift_recovered_reference/coinft_control_reference/official_coinft_github/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX
```

通过标准：

- 能发送 `i/s/t`。
- 能读到完整 50 字节包。
- 左右各 12 路 raw 数值变化合理。

## 正确使用顺序

1. 插 Teensy。
2. 确认串口存在。
3. 接 CoinFT 电源和 UART。
4. 运行 smoke test。
5. 运行笔记本 CoinFT capture。
6. capture 启动时发送 `i` 清状态，可选 `t`，再发送 `s` 开始。
7. 采集结束发送 `i` 停止。
