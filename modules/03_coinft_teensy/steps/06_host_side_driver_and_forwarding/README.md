# Step 06：Host 侧驱动与转发

这一层对应的是：

```text
Teensy 已经在出 raw 数据
接下来怎么进入 UMI-FT / ManipServer 那条 host 链路
```

## 这一步里有什么

### CoinFT host 驱动

- [code/coinft_driver/CoinFTBus.cpp](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/coinft_driver/CoinFTBus.cpp)
- [code/coinft_driver/CoinFTBus.h](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/coinft_driver/CoinFTBus.h)
- [code/coinft_driver/coin_ft.cpp](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/coinft_driver/coin_ft.cpp)
- [code/coinft_driver/coin_ft.h](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/coinft_driver/coin_ft.h)

### ManipServer 侧入口

- [code/manip_server/manip_server.cc](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/manip_server/manip_server.cc)
- [code/manip_server/manip_server_loops.cc](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/manip_server/manip_server_loops.cc)
- [code/manip_server/manip_server_pybind.cc](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/manip_server/manip_server_pybind.cc)

### 配置

- [code/configs/right_arm_coinft.yaml](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/configs/right_arm_coinft.yaml)
- [code/configs/right_arm_coinft_data_collection.yaml](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/steps/06_host_side_driver_and_forwarding/code/configs/right_arm_coinft_data_collection.yaml)

## 这层负责什么

`CoinFTBus` 负责：

1. 打开串口
2. 发 `i/s`
3. 读 58-byte 双 CoinFT 包
4. 做 tare
5. 读 `norm.json`
6. 跑 `ONNX`
7. 输出每侧 6D wrench

## 配置里能确认什么

从 `right_arm_coinft.yaml` 可以直接确认：

- 默认串口：`/dev/ttyACM0`
- 波特率：`115200`
- 左右 ONNX 文件路径
- 左右 norm 文件路径
- 左右 `PoseSensorTool`
- `WrenchSafety`

## 和本项目的关系

如果你后面想把 `UMIFT-datacollect` 的 CoinFT 数据链路对齐到 UMI-FT 风格，这一层就是最重要的参考实现。

## 当前还缺什么

- 你本机实际串口路径
- 你自己的采集封装脚本
- 接入主系统时确认 YAML 中的 production 模型路径在目标机器上可访问
