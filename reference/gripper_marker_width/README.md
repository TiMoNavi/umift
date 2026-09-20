# Marker 夹爪开合度估计

夹爪允许贴 marker，因此第一版采用通用 marker 视觉方案。这个方向和 UMI 原版一致。

## 本地 UMI 参考

恢复资料：

```text
external recovery checkout/umi_gripper_width_reference
```

关键文件：

```text
detect_aruco.py
04_detect_aruco.py
calibrate_gripper_range.py
gripper_width_standalone.py
umi_common_cv_util.py
example_aruco_config.yaml
```

恢复资料中的结论：

```text
UMI 使用 ArUco tags 贴在左右手指上，估计左右 tag 的 3D 位置，
用左右 tag 的 x 坐标差计算开合度，再通过校准把闭合位置映射到 0。
```

`calibrate_gripper_range.py` 中可以看到：

```text
tag_per_gripper = 6
left_id = gripper_id * tag_per_gripper
right_id = left_id + 1
width = get_gripper_width(tag_dict, left_id, right_id, nominal_z=0.072)
```

## 我们的第一版方案

1. 在左右夹爪指尖或可见侧面各贴一个 ArUco marker。
2. iPhone 夹爪视角 RGB 负责看见这些 marker。
3. 每帧检测 marker。
4. 使用相机内参估计 marker pose 或使用稳定的像素/深度几何。
5. 输出 `gripper_width_m` 和 `confidence`。

输出：

```text
gripper_width/
├── gripper_width_by_frame.csv
├── gripper_width_debug.mp4
└── gripper_width_report.json
```

CSV 草案：

```text
frame_index,iphone_pose_time_s,gripper_width_m,confidence,left_marker_id,right_marker_id,status
```

## 标定流程

建议采集一段开合标定视频：

```text
打开 -> 闭合 -> 打开，重复 10 次以上
```

从标定视频得到：

- `min_width`：闭合状态 marker 测得值。
- `max_width`：最大张开状态 marker 测得值。
- marker detection rate。
- 左右 marker id。

闭合映射：

```text
gripper_width_m = measured_width_m - closed_width_offset_m
```

## 与开始触发的关系

录制开始信号也基于夹爪开合度：

```text
检测到第一次接近闭合
-> 开始 3 秒倒计时
-> 倒计时结束时正式开始记录有效片段
```

这要求 gripper width estimator 至少能在录制前 live 工作，或者在后处理时裁剪有效片段。

第一版更稳的做法：

- iPhone 和笔记本都先完整录制。
- 后处理检测第一次接近闭合。
- 从该时刻 + 3 秒开始裁剪有效训练片段。

这样不用实时控制所有设备同时开始。

## 风险

- marker 被遮挡会导致宽度缺失。
- iPhone 视角太近时 marker 可能出画。
- 反光、运动模糊会降低检测率。
- 如果训练需要非常精确的毫米级宽度，需要做相机内参和 marker 尺寸标定。
