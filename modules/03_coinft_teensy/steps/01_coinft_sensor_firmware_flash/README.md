# Step 01：CoinFT 固件烧录

这一层处理的是 `CoinFT sensor board` 本体，不是 Teensy。

## 这一步里有什么

### 资产

- [assets/CoinFT_V2_firmware.hex](assets/CoinFT_V2_firmware.hex)
- [assets/CFT_V2_BOM_PCBA.xlsx](assets/CFT_V2_BOM_PCBA.xlsx)
- [assets/CFT_V2_CPL_revised.xlsx](assets/CFT_V2_CPL_revised.xlsx)

## 当前已知

- 公开仓库里有 `CoinFT_V2_firmware.hex`
- 这是 CoinFT 板载 MCU 的固件镜像
- 当前没有公开源码
- BOM 说明它是 `PSoC` 类 MCU
- 早期上游 API 快照已经不保留在本模块里，避免和当前 Teensy bridge / production 模型混在一起

## 当前未知

- 真实编程针脚
- 现场烧录夹具 / 连接方法
- 你手里这批板子是否已经烧好

## 建议动作

1. 先别默认重刷
2. 先确认板子是否已经有工作固件
3. 只有在板子空白或固件损坏时，再去做这一步

## 风险提醒

由于现在没有完整源码和板级烧录说明，这一步不适合作为第一优先级。当前最稳的是先把：

```text
Teensy bridge
-> raw 数据
-> host 校准链路
```

跑通。
