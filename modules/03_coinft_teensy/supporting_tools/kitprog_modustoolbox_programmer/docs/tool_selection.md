# 工具选择

## 1. KitProg3 是什么

`KitProg3` 是 Infineon / Cypress 的一套低层通信固件，用于：

- 编程
- 调试
- USB 到目标芯片通信桥接

官方文档明确写到：

- `KitProg3` 是 programming/debugging 的低层通信固件
- 它也是 `MiniProg4` 里使用的通信固件

来源：

- <https://www.infineon.com/assets/row/public/documents/30/44/infineon-kitprog3-user-guide-usermanual-en.pdf>

## 2. MiniProg4 是什么

`MiniProg4` 是独立的 program/debug probe。

和 `KitProg3` 的关系可以这样理解：

```text
KitProg3 = 通信固件 / 能力层
MiniProg4 = 带这种能力的独立硬件探针
```

官方文档明确说：

- `MiniProg4` 是 protocol translation device
- 支持通过 `SWD` / `JTAG` 给目标器件编程和调试

来源：

- <https://www.infineon.com/dgdl/Infineon-CY8CKIT-005_MINIPROG4_PROGRAM_AND_DEBUG_KIT_GUIDE-UserManual-v04_00-EN.pdf?fileId=8ac78c8c7d0d8da4017d0f011df41849>

## 3. 对 CoinFT 这件事，优先选哪个

### 推荐：MiniProg4

原因：

1. 它是独立探针，不依赖你手头另一个开发板
2. 官方明确给了 `5-pin / 10-pin SWD` 连接说明
3. 更适合接一块新做出来的自定义板

### 次选：某块开发板上的 KitProg3

这条路理论上可行，但问题是：

1. 不同开发板暴露的编程口不一定一样
2. 你还得确认那块板上的 KitProg3 是否能拿来接外部 target
3. 现场接线和电源边界更容易出坑

所以如果只是为了给 CoinFT 板子烧录：

```text
MiniProg4 更像正经工具
KitProg3 更像可以借用，但要看板卡实现
```

## 4. 对这个项目的建议

如果后面你真要把 CoinFT 板子本体烧起来，建议采购 / 借用优先级：

1. `MiniProg4`
2. 能确认外部 SWD 可用的 `KitProg3` 开发板
