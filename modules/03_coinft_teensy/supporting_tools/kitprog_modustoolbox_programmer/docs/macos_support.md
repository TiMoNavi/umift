# macOS 支持情况

## 短答案

`macOS` 可以跑，但你要看机器是 `Apple Silicon` 还是 `Intel`。

## 1. ModusToolbox Programming tools 官方页面

Infineon 当前产品页写的是：

- Windows 10/11
- macOS Monterey / Ventura / macOS 14
- 提到 `Intel and ARM processors via Rosetta`

来源：

- <https://www.infineon.com/design-resources/development-tools/sdk/modustoolbox-software/modustoolbox-programming-tools>

## 2. fw-loader 官方文档

`fw-loader` 文档页当前写的是支持：

- Windows
- Ubuntu
- macOS Ventura / Sonoma / Sequoia

来源：

- <https://documentation.infineon.com/modustoolbox/docs/introduction-launch-fw-loader-tool>

## 3. 最新发布说明里的变化

Infineon 社区 `2026-05` 的发布说明又明确写到：

```text
Apple Silicon 原生 ARM64 支持
Intel-based macOS 不再支持
```

来源：

- <https://community.infineon.com/t5/ModusToolbox/Release-Announcement-ModusToolbox-Ecosystem-Updates-Tools-Package-3-8-VS-Code/td-p/1217505>

## 4. 我对当前状态的判断

官方页面之间有一点版本不同步：

- 产品页还残留了 `Intel + ARM via Rosetta` 说法
- 更新发布说明已经推进到 `Apple Silicon native`，并说 `Intel macOS` 不再支持

因此当前最稳的判断是：

### 如果你的 Mac 是 Apple Silicon

```text
可以作为首选开发机
```

### 如果你的 Mac 是 Intel

```text
不要把它当首选
即使旧页面看起来还写了支持
```

## 5. 对这个项目的建议

如果你准备用 Mac 去做 CoinFT 板载固件烧录：

1. 优先确认机器是不是 Apple Silicon
2. 先安装 `ModusToolbox Programming tools`
3. 先单独跑 `fw-loader` 看探针能否识别
4. 再进 GUI 做 program

## 6. 这不等于 CoinFT 一定能直接烧成功

`macOS` 侧工具可用，只解决了：

- 探针驱动/工具链
- 探针固件升级
- 程序下载能力

但 CoinFT 本体能否烧成功，仍然还取决于：

- CoinFT 板子的 SWD 引出
- 板上供电
- 目标 MCU 型号
- `.hex` 是否和那块板完全匹配
