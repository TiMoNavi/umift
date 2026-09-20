# ForceFlow Zarr Exporter 计划

目标：从一个或多个已对齐 run 生成：

```text
export/<task>.zarr/
```

并满足：

- `docs/training_zarr_alignment.md`
- `docs/training_format_implementation.md`
- `schemas/forceflow_zarr_schema.md`
- `/Users/550m/code/ForceFlow/scripts/validate.py`

## 输入

每个 episode 输入来自 run 目录：

```text
iphone_gripper/
global_camera/
coinft/
gripper_width/
aligned/
RUN_METADATA.json
```

导出器不直接修改原始数据。

## 处理步骤

1. 读取 `aligned/aligned_index.csv`。
2. 使用 `effective_start_time` 裁剪有效片段。
3. 逐 step 读取 iPhone RGB，生成 `rgb_arm`。
4. 逐 step 读取 D435 RGB，生成 `rgb_fix`。
5. 将 iPhone pose 转成 `(N, 6)` `pos`。
6. 将 CoinFT wrench 映射成 `(N, 6)` `force`。
7. 将 gripper width 阈值化成 `gripper_state`。
8. 用相邻 pose 生成 `action`。
9. 用相邻 gripper state 生成 `gripper_action`。
10. 写 `episode` 和 `meta/episode_ends`。
11. 统计 normalizer 并写 `<task>_normalizer.json`。
12. 调用 ForceFlow validator。

## 第一版未定决策

导出器开始实现前必须确定：

- `pos` 的 6D 表示：例如 `[x,y,z,rx,ry,rz]`，旋转向量单位为 rad。
- `action` 的 6D 表示：绝对下一 pose、delta pose、还是速度/增量。
- `force` 使用 left、right、还是 fused。
- `force_frame` 使用 CoinFT frame 还是 TCP frame。
- `gripper_width_m` 的 close threshold。

这些选择必须写入 zarr metadata 或旁路 `export_metadata.json`，便于训练结果追溯。

## 当前脚本

### 1. 对齐 run 到 iPhone 时间轴

```bash
python tools/alignment/align_run_to_iphone.py runs/<run_id> --force-source left
```

或：

```bash
python tools/alignment/align_run_to_iphone.py runs/<run_id> --force-source right
```

输出：

```text
runs/<run_id>/aligned/
├── aligned_index.csv
├── aligned_force.csv
├── aligned_pose.csv
├── ALIGNMENT_REPORT.json
└── ALIGNMENT_REPORT.md
```

### 2. 导出 ForceFlow Zarr

```bash
python tools/export/export_forceflow_zarr.py runs/<run_id> --task <task_name>
```

输出：

```text
runs/<run_id>/export/<task_name>.zarr/
├── data/
├── meta/
├── <task_name>_normalizer.json
└── export_metadata.json
```

当前脚本是第一版骨架，当前约定：

- `pos = [x,y,z,rx,ry,rz]`
- `action = pos[i+1] - pos[i]`
- `gripper_action = next gripper_state`
- `force_frame = coinft`
- `force_source` 第一版只用 `left` 或 `right`

后续如果训练端需要不同定义，再在 exporter 里切换。
