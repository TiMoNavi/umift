# 录制触发和坐标系重置

## 录制开始信号

当前决定：以夹爪开合程度作为有效片段开始信号。

规则：

```text
第一次检测到夹爪接近闭合
-> 触发 3 秒倒计时
-> 倒计时结束
-> 有效训练片段开始
```

推荐实现方式：

1. 所有设备提前开始原始录制。
2. 后处理从 iPhone 夹爪视角估计 `gripper_width_m`。
3. 找到第一次低于阈值的时刻：

```text
gripper_width_m <= close_threshold_m
```

4. 有效开始时间：

```text
effective_start_time = first_close_time + 3.0
```

5. 裁剪或标记该时间之后的数据为训练片段。

这样比实时触发更稳，因为不会因为某一路设备启动慢而丢数据。

## 录制结束信号

建议采用“连续 3 秒拿稳夹爪”作为有效片段结束条件。

定义：

```text
在 effective_start_time 之后
iPhone 6DoF pose 连续 3 秒抖动很小
-> 判定操作者已经拿稳/停住夹爪
-> effective_end_time = stable_window_start_time
```

这样做的好处：

- 不依赖人去按停止按钮，避免最后时刻按按钮造成异常运动。
- 如果操作者忘记下一步操作、站在原地发愣，也能自动结束有效片段。
- 原始数据仍完整保存；这里只影响训练片段裁剪。

第一版建议用后处理实现，不要实时停止设备录制：

1. 所有设备继续完整录制。
2. 后处理读取 iPhone `poseTimes` 和 pose。
3. 计算滑动窗口内的平移抖动和旋转抖动。
4. 找到第一个满足稳定条件的 3 秒窗口。
5. 把该窗口开始时刻作为 `effective_end_time`。

稳定条件草案：

```text
window = 3.0 seconds
translation_range_m < 0.01
rotation_range_deg < 2.0
```

具体阈值需要根据 iPhone ARKit pose 噪声实测调整。

为了避免误杀任务中的自然停顿，建议加保护条件：

```text
effective_end_time 只能出现在 effective_start_time + min_episode_duration_s 之后
min_episode_duration_s 第一版可设为 5-10 秒
```

可选更稳策略：

```text
pose 稳定 3 秒
且 gripper_width_m 变化很小
且 CoinFT force 变化很小
```

如果只用 pose 稳定，可能会把“任务中短暂停顿观察”的片段误判为结束。第一版可以先只用 pose，但必须在报告中记录触发窗口，方便人工检查。

## close threshold

第一版先用相对阈值：

```text
close_threshold_m = closed_width_m + 0.15 * (open_width_m - closed_width_m)
```

如果已有真实夹爪宽度标定，可改成绝对阈值，例如：

```text
close_threshold_m = 0.005
```

具体数值需要标定视频确认。

## 坐标系重置

你的设想是：

```text
打开 iPhone App
按下某个录制/重置按钮
此刻手机位置作为世界坐标系原点
摄像头朝向作为正方向
```

这个方案可行，建议定义为 **session world frame**。

具体约定：

```text
T_world_phone_at_reset = Identity
world_origin = phone position at reset
world_forward = camera optical forward direction at reset
world_up = gravity-aligned up if ARKit provides gravity alignment
```

之后每一帧 iPhone pose 都保存为：

```text
T_world_phone(t) = inverse(T_arkit_phone_at_reset) * T_arkit_phone(t)
```

这样每次录制开始前都能把轨迹重置到相同局部坐标系。

## 注意边界

这个世界坐标系是 iPhone/ARKit 的 session world frame，不自动等于 D435 的相机坐标系。

如果训练格式需要把 D435 全局相机、iPhone、CoinFT/TCP 放到统一空间坐标系，后续还需要：

- D435 内参。
- iPhone 内参。
- D435 到 session world 的外参。
- 夹爪 marker / 标定板 / 手眼标定。

第一版可以先做到：

```text
iPhone pose 使用 session world
D435 数据只做时间对齐
CoinFT wrench 使用 CoinFT frame
gripper width 使用 iPhone frame 对齐
```

## App 行为建议

iPhone App 增加两个清晰动作：

1. `Reset Origin`
   - 当前 iPhone pose 作为 session world 原点。
   - 保存 reset pose 到 metadata。

2. `Record`
   - 开始原始录制。
   - 原始录制可以早于有效训练片段。

如果想减少按钮，也可以把 `Record` 的按下瞬间同时作为 reset origin。但长期看，分开更安全：先摆好手机、reset，再开始录制。
