# CoinFT 模型目录

Module 04 默认以 raw-only 启动，不会从源码目录自动加载模型。运行人员必须在 GUI 中选择模型集合路径，或启动时显式传入：

```bash
python3 entrypoints/receiver_web_gui.py \
  --coinft-config /absolute/path/to/model_set.json
```

GUI 支持三种输入：

1. 包含 `model_set.json` 的模型集合目录。
2. 包含 `left/`、`right/` 的目录，每侧恰好一个 ONNX、一个 norm JSON 和 ONNX external data。
3. 左右 ONNX 和 norm JSON 四个绝对路径。

选择过程只验证并使用输入路径，不会把模型复制到源码下的 `models/current/`。

目录职责：

```text
presets/                 已验证、可选的运行模型集合
archived/                被替换的历史模型，不用于默认运行
training_checkpoints/    训练 checkpoint，不是 ONNX 运行依赖
```

当前已验证 preset：

```text
presets/coinft_2606602_0019_0012/model_set.json
```

两个 ONNX 都引用同目录的 `CFT24_MLP.onnx.data`。不要改名或只复制 `.onnx`；右侧旧的 `coinft_2606602_0012_MLP.onnx.data` 与该文件完全重复且未被 ONNX 引用，已从运行集合移除。

CoinFT 板本身不提供可读取的电子 ID，物理左右接线必须与 `model_set.json` 中的 `hardware_label` 和 `sensor_index` 一致。
