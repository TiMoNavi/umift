# Teensy 4.1 刷写 Runbook

这个 runbook 只针对当前项目里的 Teensy 4.1 bridge 固件。

当前工程：

```text
<PATH_TO_DELIVERABLES>/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware
```

## 1. 当前固件职责

Teensy 4.1 做的是：

```text
CoinFT UART frames
-> Teensy active hardware UART detection
-> USB serial packets
-> Mac host
```

当前不是固定某两个 UART 的早期版本。固件会打开 Teensy 4.1 的 hardware UART candidates，运行时检测哪些端口真的收到 CoinFT frame。

双 CoinFT 在线时的映射：

```text
left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
right = lower numbered active Teensy Serial/pins  = coinft_2606602_0012
```

双路 USB 包：

```text
0x00 0x00
+ sequence_id uint32 little-endian
+ teensy_time_us uint32 little-endian
+ left raw 12ch uint16 little-endian
+ right raw 12ch uint16 little-endian
= 58 bytes
```

## 2. 编译

```bash
cd <PATH_TO_DELIVERABLES>/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/firmware
pio run
```

## 3. 上传

```bash
pio run -t upload
```

如果卡在等待设备，按一下 Teensy 4.1 的 `Program` 按钮。

## 4. Mac 上确认串口

```bash
ls /dev/cu.usbmodem*
ls /dev/tty.usbmodem*
```

出现新的 `usbmodem` 设备，通常说明应用固件已经运行。

## 5. Smoke Test

没有接 CoinFT 时，也可以先验证模拟双路包：

```bash
python <PATH_TO_DELIVERABLES>/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/tools/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX \
  --simulate
```

接上真实 CoinFT 后，先跑诊断：

```text
发送 d
```

诊断输出要重点看：

```text
left=<higher numbered active Serial>
right=<lower numbered active Serial>
```

再跑普通 stream smoke test：

```bash
python <PATH_TO_DELIVERABLES>/modules/03_coinft_teensy/steps/04_teensy_bridge_firmware/tools/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX
```

## 6. 不在 Teensy 上做什么

Teensy 不做 ONNX 推理，不做多源全局时间同步，也不保存最终力数据。它只提供稳定的 raw packet、`sequence_id` 和 `teensy_time_us`。Host 侧再用：

```text
assets/calibration/production/current_left_right_mapping.json
```

选择对应的 production ONNX/norm。
