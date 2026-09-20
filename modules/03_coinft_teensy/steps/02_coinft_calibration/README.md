# Step 02：CoinFT 校准

这一层处理的是：

```text
新做出来的 CoinFT
如何和参考六维力传感器一起做标定
最后产出 ONNX + norm.json
```

当前这里要分成两条理解：

1. 官方公开脚本体现出来的是 `ATI + NI DAQ` 路线
2. 你当前手上的参考传感器是 `DEDH-75D-50NX6`，这是 `RS485` 数字路线

## 这一步里有什么

### 校准脚本

- [code/coinft_data_collection.py](code/coinft_data_collection.py)
- [code/data_processor.py](code/data_processor.py)
- [code/coinft_MLP_train.py](code/coinft_MLP_train.py)
- [code/coinft_tuner.py](code/coinft_tuner.py)
- [code/h5py_visualizer.py](code/h5py_visualizer.py)

### 现有 host 侧实现参考

- [code/CoinFTBus.cpp](code/CoinFTBus.cpp)
- [code/CoinFTBus.h](code/CoinFTBus.h)
- [code/umift_ft_timestampped_UART.py](code/umift_ft_timestampped_UART.py)
- [code/wrench_calibration.py](code/wrench_calibration.py)

### 当前生产模型 / norm 资产

当前两只真实 CoinFT 的生产标定文件不再放在本步骤目录里，统一放在：

- [../../assets/calibration/production](../../assets/calibration/production)

当前映射：

```text
left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
right = lower numbered active Teensy Serial/pins  = coinft_2606602_0012
```

旧的 demo/shared 模型和不匹配的示例模型已经从这里移除，避免误用于当前硬件。

## 官方公开流程，按代码反推

### 1. 调通传感通道

先用：

- `coinft_tuner.py`

它会：

- 连 CoinFT / PSoC 串口
- 查询通道数
- 下发 tuning 参数
- 看原始通道数据是否稳定

### 2. 和 ATI 一起采集校准数据

再用：

- `coinft_data_collection.py`

从脚本可以直接确认：

- `ATI_CHANNELS = Dev1/ai0:5`
- `SYNC_CHANNEL = Dev1/port0/line1`
- `ATI_RATE = 1000`
- 通过 `nidaqmx` 采 ATI
- 通过 `serial` 采 CoinFT

这就是你说的那条路：

```text
ATI 参考传感器 + CoinFT
一起受力
同步采样
```

## 你现在手上的参考传感器不是 ATI

当前已经确认你的参考件是：

```text
DEDH-75D-50NX6
Fx/Fy/Fz = 50N
Tx/Ty/Tz = 2Nm
RS485
Modbus-RTU + 主动发送协议
```

所以要把这里拆开看。

### A. ATI 官方脚本路线

优点：

- 这是目前公开代码里最直接可证的 CoinFT 校准实现
- 同步线和 `nidaqmx` 路径写得很明确

限制：

- 强依赖 `NI DAQ`
- 强依赖 ATI 风格模拟输出和标定矩阵

### B. DEDH 数字参考传感器路线

你这只 DEDH 可以作为参考真值来源，但不能直接跑 `coinft_data_collection.py`。

原因很简单：

- `coinft_data_collection.py` 里写死了 `nidaqmx`
- 写死了 `Dev1/ai0:5`
- 写死了 `ATI_CAL_MAT`
- 写死了 `SYNC_CHANNEL`

对 DEDH 来说，应该变成：

```text
DEDH 六维力传感器
-> USB-RS485
-> Modbus-RTU 或主动发送协议
-> 上位机读取 Fx/Fy/Fz/Mx/My/Mz
-> 再和 CoinFT raw 12ch 做时间对齐
```

详细说明见：

- [../../supporting_tools/dedh_reference_sensor](../../supporting_tools/dedh_reference_sensor)
- 第一阶段可直接先跑：
  - [../../supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py](../../supporting_tools/dedh_reference_sensor/scripts/dedh_modbus_reader.py)

### 3. 数据处理

然后用：

- `data_processor.py`

它会：

- 读取 `*_train.h5 / *_val.h5 / *_test.h5`
- 计算 `mu_x / sd_x / mu_y / sd_y`
- 输出 `norm.json`

### 4. 训练模型

然后用：

- `coinft_MLP_train.py`

它会：

- 读取处理后的 `train/val/test`
- 训练 `MLP`
- 输出 `.pth`
- 导出 `.onnx`

## 旧流程和新流程

### 论文里写的

```text
2nd-order least squares fit
```

### 官方代码现在提供的

```text
tuning
-> ATI+CoinFT data collection
-> data processor
-> norm.json
-> MLP train
-> ONNX
```

所以当前最稳的理解是：

- 论文描述的是早期/概念上的校准方式
- 公共仓库脚本体现的是他们后来的实操流程

## 和 UMIFT / UMI-FT 的关系

UMI-FT 本身不负责教你怎么校准新做出来的 CoinFT。它只消费：

```text
raw 12ch
-> tare
-> normalize
-> ONNX
-> 6D wrench
```

## 当前还缺什么

- 如果走 ATI 路线：现场 ATI / CoinFT / sync line 接线照片或针脚确认
- 如果走 DEDH 路线：`RS485` 接线定义、主动发送帧格式、vendor 调试软件信息
- 真机采集时复核 `current_left_right_mapping.json` 和实际接线完全一致

## 如果你要真的做一只新 CoinFT 的校准

建议顺序：

1. 先跑 `coinft_tuner.py`
2. 如果你用 ATI，跑 `coinft_data_collection.py`
3. 如果你用 DEDH，先做 DEDH reader 和时间同步采集
4. 再跑 `data_processor.py`
5. 再跑 `coinft_MLP_train.py`
6. 最终把导出的 `onnx + norm.json` 收进 `assets/calibration/production/<coinft_id>/`
