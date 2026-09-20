# Step 03：Teensy 基本信息

这一层只回答一个问题：

```text
Teensy 4.1 在这条链路里到底负责什么
```

## 角色

Teensy 不是校准器，也不是最终推理端。

它负责：

```text
CoinFT UART
-> Teensy
-> USB serial
-> Mac
```

## 当前分工

```text
Teensy hardware UART candidates -> CoinFT left/right
USB Serial                      -> Mac
```

当前固件会打开 Teensy 4.1 的硬件 UART 候选池，并按运行时收到的合法 CoinFT frame 自动接入。

两块 CoinFT 同时在线时，当前项目约定是：

```text
编号更高的 active Teensy Serial/pins -> left
编号更低的 active Teensy Serial/pins -> right
```

例如当前现场描述是“左边接在编号更高的针脚，右边接在编号更低的针脚”，这正好匹配固件输出顺序：

```text
USB packet payload = left raw 12ch + right raw 12ch
```

不要再按早期固定两路 UART 的写法理解当前固件。

## 接线原则

```text
Teensy RX <- CoinFT TX
Teensy TX -> CoinFT RX
GND       <-> GND
```

电平一定先确认 `3.3V TTL`。

## 当前最值得先验证的事

1. Mac 能看到 Teensy 串口
2. Teensy 4.1 能成功刷 bridge 固件
3. 没接 CoinFT 时，串口也能正常打开

这一步的详细刷写和桥接代码在 Step 04。
