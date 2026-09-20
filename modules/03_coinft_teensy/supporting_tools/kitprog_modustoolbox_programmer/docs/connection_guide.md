# 如何连接烧录器

这一页只讨论“烧录 CoinFT sensor board 本体”。

不讨论 Teensy。

## 1. 你至少需要哪些信号

对 `PSoC` 这类目标，最核心的是 `SWD` 这几根线：

```text
SWDIO
SWCLK
XRES
GND
VTARG
```

对一块自定义板来说，最重要的是：

- 板上得把这些点引出来
- 你得知道它们在板子上的实际焊盘 / 排针位置

## 2. MiniProg4 官方支持什么连接

MiniProg4 官方文档确认：

- 支持 `5-pin` 或 `10-pin` 的 `SWD`
- `PSoC 4 / PSoC 5LP / PSoC 6` 可以通过这些接口编程调试

并且表里直接写了：

- `SWDIO`
- `SWCLK`
- `XRES`

对应 5-pin / 10-pin 信号位

来源：

- MiniProg4 guide，`SWD/JTAG`、`5-pin`、`10-pin` 小节

## 3. 对 CoinFT 板子的实际接法建议

当前最安全的现场策略是：

```text
MiniProg4 / KitProg3
SWDIO -> CoinFT target SWDIO
SWCLK -> CoinFT target SWCLK
XRES  -> CoinFT target XRES
GND   -> CoinFT target GND
VTARG -> CoinFT target I/O reference voltage
```

### `VTARG` 是干嘛的

它不是单纯“随便接个电源”。

它主要让烧录器知道目标板的 I/O 电压参考。

很多情况下更稳的是：

- 目标板自己供电
- 烧录器接 `VTARG` 做电压参考感知

而不是默认让烧录器给板子供电。

## 4. MiniProg4 能不能给板子供电

官方文档写了：

- `MiniProg4` 可通过 USB 供电
- 在单电源板上，它也可以给目标板供电
- 但供电能力大约只有 `200 mA`

所以对 CoinFT 这种自定义板：

```text
除非你很确定板子电流和电源路径都安全
否则优先让目标板自供电
MiniProg4 只负责编程和参考电压检测
```

## 5. 当前对 CoinFT 板子还缺的关键信息

我们现在还没有从公开资料里确认到：

1. CoinFT 板上 SWD 焊盘具体在哪
2. 焊盘顺序
3. 是否已经预留标准 5-pin / 10-pin 接口
4. 板子正常工作电压和烧录电压边界

所以这一步目前仍然是：

```text
工具链有了
接线原则有了
但 CoinFT 板级 pinout 还需要你现场核板子
```

## 6. 如果你想尽量少踩坑

建议顺序：

1. 先确认 CoinFT PCB 上有没有 SWD 标识
2. 用万用表 / 原理图 / 设计文件确认 `SWDIO/SWCLK/XRES/GND/VTARG`
3. 目标板自供电
4. 再接烧录器
5. 先尝试识别目标，不急着立刻 program
