# scripts

当前这里先放最小可用工具：

- `dedh_modbus_reader.py`

作用：

1. 用 `RS485 + Modbus-RTU` 先把 DEDH 读通
2. 验证 `Fx/Fy/Fz/Mx/My/Mz` 六轴值能否稳定输出
3. 提供“全部通道清零”和“切换到主动发送协议2”的命令入口

当前还没做的事：

- 主动发送协议2的数据帧解析
- DEDH 与 CoinFT 的双路同步采集器
- DEDH 原始数据直接转 CoinFT 校准训练集格式
