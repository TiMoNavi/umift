# ForceFlow Zarr Schema

本 schema 是 `docs/training_zarr_alignment.md` 的工程化摘要。导出器和 validator 以这里为硬约束。

## 目录

```text
export/<task>.zarr/
├── data/
├── meta/
└── <task>_normalizer.json
```

## 必需 datasets

| Path | Shape | Dtype |
| --- | --- | --- |
| `data/rgb_arm` | `(N, 3, 240, 320)` | `uint8` |
| `data/rgb_fix` | `(N, 3, 240, 320)` | `uint8` |
| `data/pos` | `(N, 6)` | `float32` |
| `data/force` | `(N, 6)` | `float32` |
| `data/action` | `(N, 6)` | `float32` |
| `data/gripper_state` | `(N, 1)` | `float32` |
| `data/gripper_action` | `(N, 1)` | `float32` |
| `data/episode` | `(N,)` | `uint16` |
| `meta/episode_ends` | `(M,)` | `uint32` |

所有 `data/*` 第一维必须相同。

## 图像约束

```text
input HWC RGB -> resize to H=240, W=320 -> transpose to CHW
```

不要保存 BGR。OpenCV 读取视频后通常是 BGR，导出前必须转 RGB。

## Gripper 语义

对齐 iffyuan-XArm-Toolkit / ForceFlow 现有逻辑：

```text
gripper_state = 0.0 -> closed
gripper_state = 1.0 -> open

gripper_action = 0.0 -> close
gripper_action = 1.0 -> open
```

从视觉宽度转换：

```text
gripper_state[i] = 0.0 if gripper_width_m[i] <= close_threshold_m else 1.0
gripper_action[i] = gripper_state[i + 1] for normal steps
gripper_action[last_step_of_episode] = gripper_state[last_step_of_episode]
```

如果后续有真实夹爪目标命令，优先用命令生成 `gripper_action`。

## Action 语义

`data/action` 不是 13 维。它只保存 6D 末端动作。

ForceFlow loader 会在训练时拼接：

```text
training_action = data/action(6) + data/gripper_action(1) + next_step_force(6)
```

第一版从 iPhone pose 生成：

```text
action[i] = pose_delta_6d(pos[i], pos[i + 1])
action[last_step_of_episode] = action[last_step_of_episode - 1]
```

不要跨 episode 计算 action。

## Force 语义

`data/force` 必须是稳定定义的 6D wrench。第一版必须在 exporter metadata 中记录：

```text
force_source = left_wrench | right_wrench | fused_wrench
force_frame = coinft | tcp | other
```

在没有明确 TCP 变换和 gripper width 外参前，建议先使用：

```text
force_source = fused_wrench 或 left_wrench/right_wrench 中更可信的一路
force_frame = coinft
```

一旦选定，同一个 zarr 内所有 episode 必须一致。

## Normalizer

文件名：

```text
export/<task>.zarr/<task>_normalizer.json
```

内容只需要：

```json
{
  "pos": {"max": [0, 0, 0, 0, 0, 0], "min": [0, 0, 0, 0, 0, 0]},
  "action": {"max": [0, 0, 0, 0, 0, 0], "min": [0, 0, 0, 0, 0, 0]},
  "force": {"max": [0, 0, 0, 0, 0, 0], "min": [0, 0, 0, 0, 0, 0]}
}
```

统计范围只包含有效训练片段，不包含 reset、等待、倒计时前的数据。

## Validator

第一版导出后必须通过：

```bash
python /Users/550m/code/ForceFlow/scripts/validate.py <export/<task>.zarr>
```

如果 validator CLI 参数不同，以该脚本实际入口为准。
