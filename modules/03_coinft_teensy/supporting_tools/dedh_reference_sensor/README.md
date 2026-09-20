# DEDH 参考力传感器

这一组文档记录你手上的参考六维力传感器，而不是 CoinFT 本体。

当前确认型号前提：

```text
DEDH-75D-50NX6
Fx/Fy/Fz = 50N
Tx/Ty/Tz = 2Nm
接口 = RS485
协议 = Modbus-RTU + 主动发送协议
```

资料来源：

- 桌面说明书 PDF：`local DEDH manual PDF (not included)`
- 桌面说明书文本：`local DEDH manual text (not included)`

## 这里回答什么

1. 这只 DEDH 和 ATI 的差别是什么
2. 它能不能当 CoinFT 校准时的参考传感器
3. 如果能，哪些官方脚本可以直接复用，哪些不能
4. 现在还缺哪些 vendor 侧信息

## 建议阅读顺序

1. [docs/spec_summary.md](docs/spec_summary.md)
2. [docs/coinft_calibration_substitution.md](docs/coinft_calibration_substitution.md)
3. [docs/modbus_reader_usage.md](docs/modbus_reader_usage.md)
4. `open_questions.md` 未随本包提供；现场问题以 `docs/spec_summary.md` 和实际硬件为准。
