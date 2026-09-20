# tools 目录说明

## 交付包路径检查

从交付包根目录运行：

```bash
python3 tools/validate_deliverable_paths.py
```

脚本会检查 Markdown 链接、JSON/YAML 模型/固件路径、包外绝对路径、占位符，以及 `.pio`、`.DS_Store`、`.bak` 等生成或备份文件。为避免遍历采集数据，脚本会跳过 `runs/`；第三方源码树也不会作为交付源码路径检查。

这个目录放的是实际脚本实现。

简单说：

```text
modules/ = 工作入口
docs/    = 规则说明
tools/   = 真正执行的脚本
```

## 当前子目录

- `capture/`
  - 采集脚本预留位置
- `gripper_width/`
  - 视觉夹爪宽度脚本预留位置
- `alignment/`
  - 时间轴对齐脚本
- `export/`
  - 训练导出脚本

## 当前已有脚本

- `alignment/align_run_to_iphone.py`
- `export/export_forceflow_zarr.py`

说明：

- 当前 Mac 侧新增的软件入口已改到 `modules/04_laptop_alignment_export/`
- `tools/capture/` 仍然是预留位置，但本轮实现不再放代码到这里

## 怎么使用这个目录

如果你在实现某个模块：

1. 先看 `modules/<module>/README.md`
2. 再看相关 `docs/` 规则
3. 最后改这里的脚本

这样不容易一上来就陷进脚本细节里。
