# scripts

这个目录预留给 CoinFT + Teensy 的本模块脚本。

当前脚本按两层分开：

```text
collect_coinft_raw.py
  忠实采集层。只打开 Teensy 串口，按 58-byte bridge 协议保存：
  - raw_coinft_packets.bin
  - raw_coinft_stream.csv
  - capture_log.json

convert_coinft_raw.py
  格式转换层。从 raw_coinft_stream.csv 派生 LF/RF CSV。
  给定 ONNX/norm 后才输出 Fx/Fy/Fz/Mx/My/Mz。
```

## 采集

```bash
python3 modules/03_coinft_teensy/scripts/collect_coinft_raw.py \
  --port /dev/cu.usbmodemXXXX \
  --duration-s 10 \
  --output-dir runs/<run_id>/coinft/raw
```

如果 CoinFT 还没接，可以让 Teensy 固件输出模拟包：

```bash
python3 modules/03_coinft_teensy/scripts/collect_coinft_raw.py \
  --port /dev/cu.usbmodemXXXX \
  --simulate \
  --duration-s 10 \
  --output-dir /tmp/coinft_raw_sim
```

## 转换

只拆左右 raw 通道：

```bash
python3 modules/03_coinft_teensy/scripts/convert_coinft_raw.py \
  runs/<run_id>/coinft/raw/raw_coinft_stream.csv \
  --raw-only \
  --output-dir runs/<run_id>/coinft/converted
```

带标定转换为 ForceFlow 可用 wrench：

```bash
python3 modules/03_coinft_teensy/scripts/convert_coinft_raw.py \
  runs/<run_id>/coinft/raw/raw_coinft_stream.csv \
  --output-dir runs/<run_id>/coinft/converted \
  --left-model modules/03_coinft_teensy/assets/calibration/production/coinft_2606602_0019/coinft_2606602_0019_MLP.onnx \
  --right-model modules/03_coinft_teensy/assets/calibration/production/coinft_2606602_0012/coinft_2606602_0012_MLP.onnx \
  --left-norm modules/03_coinft_teensy/assets/calibration/production/coinft_2606602_0019/coinft_2606602_0019_norm.json \
  --right-norm modules/03_coinft_teensy/assets/calibration/production/coinft_2606602_0012/coinft_2606602_0012_norm.json
```

## 边界

- 采集脚本不做时间对齐、不做训练格式、不依赖 ONNX。
- 转换脚本不碰硬件，只消费采集产物。
- 三路同步汇总脚本应作为独立层：监听 iPhone 录制信号后启动/标记三路采集，把实时行写入缓冲区，同时落盘。
