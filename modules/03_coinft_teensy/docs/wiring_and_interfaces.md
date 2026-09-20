# CoinFT / Teensy 接线与接口说明

本文档先把“应该怎么接”和“哪些地方还需要现场确认”分开写清楚。

## 1. 当前目标拓扑

```text
CoinFT left/right -> Teensy hardware UART candidate ports
Teensy USB   -> Mac
```

角色划分：

- `CoinFT`：输出原始 12 通道数据
- `Teensy 4.1`：做 UART 到 USB 串口桥接
- `Mac`：发控制命令、读原始包、保存数据、后续跑标定模型

## 2. 当前已确认的软件接口

### 主机到 Teensy

```text
i = idle
s = start real CoinFT streaming
t = tare
m = start simulated CoinFT-like streaming
```

### Teensy 到主机

```text
0 个真实 CoinFT:
  58-byte 模拟双路包

1 个真实 CoinFT:
  直接透传该 CoinFT 的 24-byte frame body

2 个真实 CoinFT:
  header: 0x00 0x00
  metadata: sequence_id uint32 little-endian + teensy_time_us uint32 little-endian
  payload: left 12 x uint16 little-endian + right 12 x uint16 little-endian
  packet size: 58 bytes

baud: 115200
```

### CoinFT 到 Teensy

```text
UART baud = 1000000
frame start byte = 0x02
frame body = 24 bytes
frame end byte = 0x03
```

这是从已恢复的 `teensy_coinft_serial_interface.cpp` 行为反推出来的。

## 3. 接线原则

在没有最终核 pinout 前，先只写确定不会错的原则：

```text
Teensy RX <- CoinFT TX
Teensy TX -> CoinFT RX
GND       -> Common GND
Power     -> 按 CoinFT 实际需求单独确认
```

必须先确认：

- CoinFT UART 逻辑电平是不是 `3.3V TTL`
- Teensy 4.1 这一侧是否能直接安全对接

当前不建议在没确认电平前直接把 `5V TTL` 接上。

## 4. Teensy 4.1 串口分工

当前 bridge 源码会打开 Teensy 4.1 的硬件 UART 候选池：

```text
Serial1..Serial8 @ 1000000 baud
```

任何一路收到至少 2 个合法 CoinFT frame 后自动接入。接入两路或更多时：

```text
Serial 端口号大的 CoinFT -> left
Serial 端口号小的 CoinFT -> right
```

常用接线示例：

```text
Serial5 RX = pin 21
Serial5 TX = pin 20

Serial7 RX = pin 28
Serial7 TX = pin 29
```

如果 CoinFT 分别插在 20/21 和 28/29，则成功接入后默认：

```text
Serial7 / pins 28,29 -> left
Serial5 / pins 20,21 -> right
```

但现场仍然建议继续确认：

1. 你实际采用的是默认引脚还是备用引脚
2. 走线空间是否更适合其它可选引脚
3. 当前 bridge 源码的候选 UART 池是否包含你实际使用的那组引脚

如果后面你决定改用 alternate pins，要把实际 pin map 一并写进运行记录。

## 5. left / right 约定

当前项目里强约束是：

```text
采集时保留 left / right 两路
第一版不做 fusion
导出时显式选择 left 或 right
```

因此接线时必须从第一天就记清：

- 哪个 CoinFT 被定义为 `left`
- 哪个 CoinFT 被定义为 `right`
- 它们分别插到哪一个 Teensy hardware Serial 端口

建议后面真实接线时，在 `RUN_METADATA.json` 或 capture log 里显式记录：

```text
left_sensor -> higher numbered active Serial port
right_sensor -> lower numbered active Serial port
```

或者相反，但一定要写死。

## 6. Mac 侧接口边界

Mac 不应该依赖 Teensy 内部实现细节，只需要依赖下面这些外部接口：

1. 串口设备路径，例如 `/dev/cu.usbmodemXXXX`
2. 控制命令 `i/s/t/m`
3. 0/1/2 CoinFT 三态输出规则
4. 每个包里的 `sequence_id` / `teensy_time_us`
5. 每个包里左右各 12 通道的顺序

也就是说，Mac capture 脚本应该能把 Teensy 看作一个“二进制串口数据源”。

## 7. 当前待确认项

下面这些还需要你拿到实物后现场补全：

1. CoinFT 电源需求
2. CoinFT 接口定义和线序
3. Teensy 4.1 最终使用了哪些 hardware Serial 引脚
4. 是否需要额外电平转换
5. 两个 CoinFT 的左右标签和序列号

## 8. 建议现场记录模板

后面硬件到位时，建议至少补下面这份记录：

```text
CoinFT left serial/model:
CoinFT right serial/model:
left connected to Teensy:
right connected to Teensy:
power source:
logic level verified:
smoke test passed:
```

这样后面排问题会轻松很多。
