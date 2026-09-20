# CoinFT runtime configs

这个目录放主采集链路会直接读取的运行配置。

## `coinft_04_laptop_calibrated.json`

给 `modules/04_laptop_alignment_export` 使用的过渡 CoinFT 校准配置：

```bash
cd ${UMIFT_ROOT}/modules/04_laptop_alignment_export
python3 entrypoints/receiver_web_gui.py \
  --host 127.0.0.1 \
  --port 8899 \
  --no-open \
  --coinft-port /dev/cu.usbmodem183837501 \
  --coinft-config ${UMIFT_ROOT}/modules/03_coinft_teensy/configs/coinft_04_laptop_calibrated.json
```

当前左右规则：

```text
left  = higher numbered active Teensy Serial/pins = coinft_2606602_0019
right = lower numbered active Teensy Serial/pins  = coinft_2606602_0012
```

这里直接写 `model_path` 和 `norm_path`，没有让 04 读取 03 的 production `manifest.json`。原因是 04 当前的 manifest loader 期待 `hardware_label/model_path/norm_path` schema，而 03 的 production manifest 是归档/审计 schema。

长期目标见：

- [../docs/coinft_to_module04_integration.md](../docs/coinft_to_module04_integration.md)

正式集成后，默认模型应放到 04 的 receiver model store：

```text
${UMIFT_ROOT}/modules/04_laptop_alignment_export/
  umift_laptop_alignment/capture/receivers/coinft/models/current/
```

主流程启动时优先自动发现 `models/current/model_set.json`；如果缺失，再通过 GUI 让用户选择/上传 left/right 模型并复制进这个目录。
