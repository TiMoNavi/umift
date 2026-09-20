# Part 03：夹爪开合度 Marker 视觉方案

这一部分只讨论：

- 夹爪开合度怎么估计
- 标记贴哪里
- UMI 原版有没有公开过方案
- 对当前这套真实安装视角，第一版该怎么做

## 结论先说

对于当前这套安装方式：

- 纯 CV 边缘/轮廓法不推荐作为第一版
- 第一版建议直接贴 `ArUco marker`
- `ARKit` 对相机 pose 有用
- `ARKit` 不适合作为夹爪开合度的主测量通道

## 先看这些

- [marker_strategy_from_umi.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/marker_strategy_from_umi.md)
- [marker_layout_for_current_mount.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/marker_layout_for_current_mount.md)
- [marker_print_and_mount_quickstart.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/marker_print_and_mount_quickstart.md)
- [printables/README.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/printables/README.md)
- [vision_strategy.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/vision_strategy.md)
- [vision_width_pipeline.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/03_gripper_marker_vision/vision_width_pipeline.md)
