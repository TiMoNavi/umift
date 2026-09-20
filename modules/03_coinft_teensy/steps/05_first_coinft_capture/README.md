# Step 05：第一次抓 CoinFT

这一层不追求“完整系统”，只追求：

```text
第一次把 CoinFT 数据从 Teensy 稳定抓到 Mac
```

## 这一步里有什么

### 脚本

- [scripts/coinft_teensy_smoke_test.py](scripts/coinft_teensy_smoke_test.py)
- [scripts/umift_ft_timestampped_UART.py](scripts/umift_ft_timestampped_UART.py)
- [scripts/capture_coinft_official_format.py](scripts/capture_coinft_official_format.py)

### 配置参考

- [configs/right_arm_coinft.yaml](configs/right_arm_coinft.yaml)
- [configs/right_arm_coinft_data_collection.yaml](configs/right_arm_coinft_data_collection.yaml)

## 先跑哪个

### 第一步

先跑：

`coinft_teensy_smoke_test.py`

用途：

- 只验证 Teensy bridge 和 58-byte 协议
- 不验证 ONNX
- 不验证最终 wrench

### 第二步

再跑：

`umift_ft_timestampped_UART.py`

用途：

- 正式读 `left/right raw 12ch`
- 做 tare
- 接 norm + ONNX
- 生成每侧 CSV

如果你只是想在这个仓库里少记一点参数，可以改跑：

`capture_coinft_official_format.py`

它只是一个薄包装，内部仍然调用官方风格的 `umift_ft_timestampped_UART.py`，不改输出格式。

## 官方格式约定

这一层现在明确以官方脚本输出为准，不额外发明你自己的 CoinFT CSV 格式。

官方脚本输出的每侧列是：

```text
Timestamp,Fx,Fy,Fz,Mx,My,Mz,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12
```

## 你从这一步应该拿到什么

至少先拿到：

```text
raw packets
left raw channel stream
right raw channel stream
```

如果标定文件齐了，再进一步拿到：

```text
Fx,Fy,Fz,Mx,My,Mz
```

## 推荐命令

### 先做桥接健康检查

```bash
python scripts/coinft_teensy_smoke_test.py --port /dev/cu.usbmodemXXXX
```

如果 CoinFT 还没接，可以先让 Teensy 自己生成模拟 raw 流：

```bash
python scripts/coinft_teensy_smoke_test.py \
  --port /dev/cu.usbmodemXXXX \
  --simulate
```

### 再做官方格式采集

```bash
python scripts/capture_coinft_official_format.py \
  --port /dev/cu.usbmodemXXXX \
  --duration 10 \
  --result-dir /tmp/coinft_capture \
  --left-model ../../assets/calibration/production/coinft_2606602_0019/coinft_2606602_0019_MLP.onnx \
  --right-model ../../assets/calibration/production/coinft_2606602_0012/coinft_2606602_0012_MLP.onnx \
  --left-norm ../../assets/calibration/production/coinft_2606602_0019/coinft_2606602_0019_norm.json \
  --right-norm ../../assets/calibration/production/coinft_2606602_0012/coinft_2606602_0012_norm.json
```

上面的 left/right 路径使用当前生产映射：高编号 Teensy pins 为 left/`coinft_2606602_0019`，低编号 Teensy pins 为 right/`coinft_2606602_0012`。

## 当前风险

`umift_ft_timestampped_UART.py` 默认假设：

- 你已经有可用 `left/right onnx`
- 你已经有可用 `left/right norm.json`

所以在标定资产没配齐之前，这一步最保险的目标仍然是：

```text
先确认 raw 12ch 通了
```
