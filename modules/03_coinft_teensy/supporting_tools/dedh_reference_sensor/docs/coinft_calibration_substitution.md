# 用 DEDH 替代 ATI 做 CoinFT 校准

## 结论先说

可以推进，但不能把它当成“ATI 换个牌子”。

更准确的说法是：

```text
ATI 路线 = 模拟参考传感器 + NI DAQ + nidaqmx
DEDH 路线 = 数字参考传感器 + RS485 + Modbus/主动发送
```

两条路的“参考真值”都是六维力/力矩，但采集链路完全不同。

## 为什么 50N 这档是合理的

你当前确认的是 `DEDH-75D-50NX6`。

对 CoinFT 这类小型六维力传感器标定来说，低量程参考传感器通常有两个现实好处：

- 在小力段里分辨率更有用
- 不容易把大部分校准数据都压缩在量程底部

但要注意，最后仍然要看三件事：

1. 实际噪声
2. 零点漂移
3. 坐标系和安装偏置是否处理干净

## 官方 CoinFT 校准脚本里哪些地方是 ATI 专属

见：

- [../../steps/02_coinft_calibration/code/coinft_data_collection.py](../../../steps/02_coinft_calibration/code/coinft_data_collection.py)

当前脚本把下面这些写死了：

- `import nidaqmx`
- `ATI_CHANNELS = Dev1/ai0:5`
- `SYNC_CHANNEL = Dev1/port0/line1`
- `ATI_RATE = 1000`
- `ATI_CAL_MAT = ...`

这意味着它现在做的是：

```text
CoinFT 串口采样
+ ATI 模拟电压采样
+ NI 数字线同步
-> 对齐
-> 生成校准训练数据
```

所以这个脚本不能直接拿来读 DEDH。

## DEDH 路线应该怎么改

### 路线 A：先用 Modbus 低速打通

适合第一阶段验证：

```text
Mac
-> USB-RS485
-> DEDH
```

同时保留：

```text
Mac
-> USB serial
-> CoinFT / Teensy
```

先确认以下事情：

1. 能稳定读出 `Fx/Fy/Fz/Mx/My/Mz`
2. 单位确实是 `N` 和 `Nm`
3. 清零命令可用
4. 采样时间戳能落盘

缺点是 Modbus 文档自己也承认实时数据速率偏低，每秒大概 `20-30` 组。

### 路线 B：切到主动发送协议做同步采集

如果要真的做校准训练，更像应该走这条：

```text
DEDH 主动发送
+ CoinFT 串口流
-> 两路时间戳对齐
-> 形成 reference wrench / raw channels 对
```

这一条的关键收益是参考传感器吞吐更高，说明书写到可达 `640/s`。

## 需要补的适配工作

### 1. 参考传感器读取器

需要新写一个 DEDH reader，功能至少包括：

- 打开 `USB-RS485` 串口
- 轮询 `Modbus-RTU` 或切换到主动发送协议
- 解析 `Fx/Fy/Fz/Mx/My/Mz`
- 给每帧打 host 时间戳
- 落盘成训练前原始记录

### 2. CoinFT 与 DEDH 的同步方法

ATI 路线有独立 `SYNC_CHANNEL`。

DEDH 路线目前还没有从说明书里看到“外部硬件同步脚”证据，因此先按下面的优先级理解：

1. 优先考虑两路串口时间戳对齐
2. 如果后面 vendor 能提供触发/同步支持，再升级
3. 先不要假设它天然等价于 NI 的数字同步线

### 3. 坐标系对齐

只要参考传感器不是 ATI，最容易埋雷的往往不是精度表，而是：

- 轴方向定义
- 安装面正反
- 力矩参考点
- CoinFT 与参考传感器的间距补偿

官方 ATI 脚本里有：

- `M_ARM = 0.0115`

这说明他们本来就在补偿参考传感器面到 CoinFT 面之间的力臂。

换成 DEDH 后，这个几何补偿仍然要重新确认。

### 4. 校准后处理

一旦你拿到了成对数据：

```text
CoinFT raw 12ch
<-> DEDH reference 6D wrench
```

后面的这些步骤原则上还能延续：

- `data_processor.py`
- `coinft_MLP_train.py`
- `norm.json`
- `ONNX`

也就是说，真正要重做的核心主要是“参考传感器采集与同步”这一段。

## 最务实的推进顺序

1. 先确认 DEDH 的接线定义和 USB-RS485 转接方案
2. 先用 `Modbus-RTU` 读通 6 轴值
3. 再确认 vendor 主动发送协议是否更适合校准
4. 单独做一版 `dedh_capture.py`
5. 再把它接进 CoinFT 校准数据采集流程
