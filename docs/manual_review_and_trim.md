# 人工复核与裁剪规范

自动开始、自动结束、单侧 force 导出，这三件事都很实用，但都不该完全黑箱。这个文档定义当自动结果不理想时，人工怎么接管。

## 相关文件

每个 run 建议有：

```text
runs/<run_id>/
├── RUN_METADATA.json
├── MANUAL_TRIM.json
└── aligned/
    ├── aligned_index.csv
    ├── ALIGNMENT_REPORT.json
    └── ALIGNMENT_REPORT.md
```

其中：

- `RUN_METADATA.json` 记录设备事实
- `MANUAL_TRIM.json` 记录人工 override
- `ALIGNMENT_REPORT.*` 记录自动对齐结果

## 什么时候需要人工复核

出现下面任一情况，就应该人工看一眼：

1. `effective_start_time` 明显早了或晚了
2. `effective_end_time` 把任务中途停顿误判成结束
3. marker 丢失导致 `gripper_state` 抖动
4. depth 文件缺失或明显不同步
5. 你不确定这次 run 应该导出 `left` 还是 `right`

## 复核顺序

建议顺序：

1. 先看 iPhone RGB
2. 再看 `gripper_width_debug.mp4`
3. 再看 `ALIGNMENT_REPORT.json`
4. 最后决定是否改 `MANUAL_TRIM.json`

## 人工可改的内容

第一版允许人工覆盖这些值：

- `effective_start_time_s`
- `effective_end_time_s`
- `close_threshold_m`
- `force_export_side`
- `review_notes`

## MANUAL_TRIM.json 建议格式

```json
{
  "use_manual_trim": false,
  "effective_start_time_s": null,
  "effective_end_time_s": null,
  "close_threshold_m": null,
  "force_export_side": null,
  "review_notes": ""
}
```

## 字段语义

### `use_manual_trim`

- `false`：默认使用自动结果
- `true`：对齐和导出时优先使用人工 override

### `effective_start_time_s`

当自动找到的“第一次接近闭合 + 3 秒”不合理时，允许人工直接给秒数。

### `effective_end_time_s`

当“pose 连续 3 秒稳定”误判时，允许人工直接给结束秒数。

### `close_threshold_m`

因为你的夹爪不是 UMI 原始夹爪，绝对宽度不能照搬别人的阈值，所以允许人工先修这个值。

### `force_export_side`

只能填：

```text
left
right
```

第一版不要填 `fused`。

## 推荐人工判定标准

### 开始

有效开始应该满足：

- 倒计时已经结束
- 操作者开始进入任务动作
- 不是刚按完录制按钮的抖动期

### 结束

有效结束应该满足：

- 任务核心动作已经完成
- 后面主要是停住、放松、发愣或收尾
- 截断后不会把最后一个真实操作动作切掉

### force 侧选择

选择哪一侧，不看“理论更高级”，先看哪一侧更稳定：

- 丢包更少
- 噪声更小
- 和任务接触事件更一致

## 当前阶段的态度

这套人工 override 不是失败兜底，而是第一版工程里很正常的一层。我们现在要的是：

- 原始数据先完整
- 自动规则先够用
- 人工修正有标准落点

这样才能又快又不乱。
