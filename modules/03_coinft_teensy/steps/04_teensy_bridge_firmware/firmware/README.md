# Teensy 4.1 buildable project

这个目录是当前项目里真正可编译的 `Teensy 4.1` bridge 工程。

## 文件

- `platformio.ini`
  - 已改成 `board = teensy41`
- `src/teensy_coinft_serial_interface.cpp`
  - 官方 bridge 源码基础上加入双路 `sequence_id` / `teensy_time_us`
  - 打开 Teensy hardware UART 候选池，运行时自动检测 0/1/2 CoinFT
  - 两块在线时，编号更高的 active Serial/pins 作为 left，编号更低的作为 right

## 用法

```bash
cd /Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware
pio run
pio run -t upload
```

如果上传时设备没有自动进 bootloader，按一下 Teensy 4.1 的 `Program` 按钮。

当前 `s` 命令的输出由运行时检测到的真实 CoinFT 数量决定：

```text
0 个真实 CoinFT:
  模拟双路包，58 bytes

1 个真实 CoinFT:
  直接透传该 CoinFT 的 24-byte frame body

2 个真实 CoinFT:
  编号更高的 active Serial/pins 作为 left
  编号更低的 active Serial/pins 作为 right
  0x00 0x00
  + sequence_id uint32 little-endian
  + teensy_time_us uint32 little-endian
  + left 24 bytes
  + right 24 bytes
```

双路包总长 `58` bytes。`teensy_time_us` 来自 Teensy 本地 `micros()`，用于后续映射到 Mac host timeline。单路调试输出不加 header 或时间戳。
