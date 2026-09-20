# Step 04：Teensy 固件与转发程序

这一层是整个 CoinFT -> Mac 链路最关键的“中间件”。

## 这一步里有什么

### 固件源码

- [firmware/src/teensy_coinft_serial_interface.cpp](firmware/src/teensy_coinft_serial_interface.cpp)
- [firmware/platformio.ini](firmware/platformio.ini)

### 测试工具

- [tools/coinft_teensy_smoke_test.py](tools/coinft_teensy_smoke_test.py)

## 这段程序做什么

它运行时自动检测真实 CoinFT 数量：

```text
0 个真实 CoinFT:
  发送模拟双路包，58 bytes

1 个真实 CoinFT:
  直接透传该 CoinFT 的 24-byte frame body
  不加 0x00 0x00、sequence_id、teensy_time_us

2 个真实 CoinFT:
  保持原来的双路包，58 bytes
  端口号/针脚编号更高的 CoinFT 作为 left
  端口号/针脚编号更低的 CoinFT 作为 right
  0x00 0x00
  + sequence_id uint32 little-endian
  + teensy_time_us uint32 little-endian
  + left 24 bytes
  + right 24 bytes
```

## 控制命令

主机发给 Teensy：

```text
i = idle
s = start CoinFT streaming with runtime 0/1/2 CoinFT detection
t = tare
m = start forced simulated CoinFT-like streaming
```

Teensy 会把 `i/s/t` 继续转发给 CoinFT。
`s` 会优先发送真实 CoinFT 数据：只有一块在线时输出 24-byte 原始 body，两块在线时输出原 58-byte 双路包。没有真实 CoinFT 在线时，输出 58-byte 模拟左右两路 raw 12 通道数据。
`m` 是本项目新增的强制模拟调试入口，不需要 CoinFT 实物。
`d` 是诊断入口，会扫描所有候选 UART 并打印哪些 Serial 端口收到合法 CoinFT frame。

## 当前要注意的地方

当前目录只保留本项目实际使用的 Teensy 4.1 工程。早期固定两路 UART 和旧板卡配置的参考文件已经删除，避免后续误用。

当前现场/模型映射约定：

```text
left  = 编号更高的 Teensy pins / active Serial = coinft_2606602_0019
right = 编号更低的 Teensy pins / active Serial = coinft_2606602_0012
```

## 推荐动作

1. 以这个目录为主工作区
2. 进入 `firmware/`
3. 直接编译
4. 直接上传
5. 跑 smoke test

命令：

```bash
cd ${UMIFT_ROOT}/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware
pio run
pio run -t upload
```

## 这一步完成的标准

```text
Mac 上能看到 usbmodem
smoke test 能读到完整 58-byte 包
0/1/2 CoinFT 场景下输出长度符合预期
```

没有接 CoinFT 时，普通 `s` 会输出模拟 58-byte 双路流：

```bash
python tools/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX
```

也可以显式跑强制模拟流：

```bash
python tools/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX \
  --simulate
```
