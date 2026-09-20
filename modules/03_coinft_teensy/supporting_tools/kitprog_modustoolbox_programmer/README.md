# KitProg / ModusToolbox Programmer 分析

这个目录单独处理 CoinFT sensor board 烧录工具链，不和主采集步骤混在一起。

重点分析：

1. `KitProg3` 是什么
2. `MiniProg4` 是什么
3. 如何把它们接到一块新做出来的 CoinFT 板子
4. 烧录器本身要不要刷固件
5. `ModusToolbox Programmer` 在 `macOS` 上能不能跑

## 快速结论

### 最推荐的路线

如果你的目标是给一块新加工出来、还没有应用固件的 CoinFT 板子烧 `.hex`：

```text
优先用 MiniProg4
其次才考虑借用某块开发板上的 KitProg3
```

原因很直接：

- `MiniProg4` 本来就是独立烧录 / 调试探针
- 官方文档明确支持 `PSoC 4 / 5LP / 6` 的 `SWD/JTAG`
- 对自定义板子接线更自然

### 烧录器要不要额外刷固件

通常不需要你手工给烧录器写“自定义固件”。

更准确地说：

- `KitProg3` / `MiniProg4` 出厂就有通信固件
- 如果版本太旧，可能需要用 `fw-loader` 做官方固件升级
- 这属于“升级烧录器自身的官方通信固件”，不是你给它开发一份新 firmware

### macOS 能不能跑

可以，但有版本细节。

当前官方信息显示：

- `ModusToolbox Programming tools` 支持 macOS
- `fw-loader` 也支持 macOS
- 但 Intel Mac 支持信息在官方不同页面之间有点不一致

截至当前查到的最新官方发布说明，`2026-05` 的更新已经写明：

```text
Apple Silicon 原生 ARM64 支持
Intel-based macOS 不再支持
```

所以：

- 如果你的 Mac 是 Apple Silicon：这条路是可行的
- 如果你的 Mac 是 Intel：不要把它当成首选方案

## 推荐阅读

1. [docs/tool_selection.md](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/supporting_tools/kitprog_modustoolbox_programmer/docs/tool_selection.md)
2. [docs/connection_guide.md](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/supporting_tools/kitprog_modustoolbox_programmer/docs/connection_guide.md)
3. [docs/firmware_update_and_modes.md](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/supporting_tools/kitprog_modustoolbox_programmer/docs/firmware_update_and_modes.md)
4. [docs/macos_support.md](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/supporting_tools/kitprog_modustoolbox_programmer/docs/macos_support.md)
5. [docs/source_links.md](/Users/550m/code/UMIFT-datacollect/modules/03_coinft_teensy/supporting_tools/kitprog_modustoolbox_programmer/docs/source_links.md)
