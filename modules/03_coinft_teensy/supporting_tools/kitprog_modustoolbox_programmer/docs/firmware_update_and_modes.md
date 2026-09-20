# 烧录器需要固件吗

短答案：

```text
需要“官方通信固件”
但通常出厂就有
你一般只需要升级，不需要自己开发
```

## 1. KitProg3

官方文档写得很明确：

- `KitProg3` 本身就是 communication firmware
- 开发板上通常已经预装
- 插上主机后，编程和调试就应该能工作

这意味着：

- 不需要你先给 `KitProg3` 写一份自定义程序
- 但可能需要升级到和工具链匹配的版本

## 2. MiniProg4

官方文档和 `fw-loader` 文档都说明：

- `MiniProg4` 也属于 KitProg3-based 设备
- 可以用 `fw-loader` 做固件升级

所以它也不是“空白探针”，而是：

```text
出厂可用
必要时做官方固件升级
```

## 3. 用什么升级

官方给的是：

- `fw-loader`

它能做：

- 列出连接的 `KitProg3` / `MiniProg4`
- 升级 firmware
- 切换 mode
- reset 设备

来源：

- <https://documentation.infineon.com/modustoolbox/docs/introduction-launch-fw-loader-tool>
- <https://www.infineon.com/fw-loaderuserguide>

## 4. 什么时候你应该先升级

下面这些情况都值得先跑一遍 `fw-loader`：

1. `ModusToolbox Programmer` 识别不到探针
2. 报 probe firmware 版本过旧
3. 设备模式不对
4. 你刚拿到一只旧的 / 来源不明的 MiniProg4

## 5. 对 CoinFT 这件事的现实建议

如果你是第一次上手：

1. 先接上 `MiniProg4` 或 `KitProg3`
2. 先用 `fw-loader` 看能不能枚举到
3. 如果能枚举，再决定要不要升级 firmware
4. 再打开 `ModusToolbox Programmer`

这个顺序比一上来就 program 一个 `.hex` 更稳。
