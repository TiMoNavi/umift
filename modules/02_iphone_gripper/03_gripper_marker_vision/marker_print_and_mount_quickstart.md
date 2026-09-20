# 夹爪 Marker 打印与贴装速查

如果你现在就要开打，先按这一版做：

## 1. 打印什么

主推荐直接打印这个：

- `printables/a4_sheet_mixed_12mm_16mm_300dpi.pdf`

如果你只想先打一种尺寸：

- `printables/a4_sheet_id0_id1_16mm_300dpi.pdf`
- `printables/a4_sheet_id0_id1_12mm_300dpi.pdf`

单张版保留给后续单独补打或局部重打：

- `printables/aruco_left_id0_16mm.png`
- `printables/aruco_right_id1_16mm.png`
- `printables/aruco_pair_id0_id1_16mm_300dpi.png`

固定参数：

- 字典：`DICT_4X4_50`
- 左夹爪：`id 0`
- 右夹爪：`id 1`
- 黑码边长：主推荐 `16 mm`，备选 `12 mm`

## 2. 如果规定点放不下

退一档，改打：

- `printables/aruco_left_id0_12mm.png`
- `printables/aruco_right_id1_12mm.png`

或者：

- `printables/aruco_pair_id0_id1_12mm_300dpi.png`

只有在 `16 mm` 明显放不下规定点时，才退到 `12 mm`。

## 3. 贴哪里

优先级固定为：

1. 直接贴在夹爪现有的 marker 规定点
2. 如果规定点不是正对相机的平面，在规定点位置加一个小平片，再把 marker 贴在平片上
3. 左右 marker 都尽量保持边缘与夹爪边缘平行

左右分配不要反：

- 左夹爪贴 `id 0`
- 右夹爪贴 `id 1`

## 4. 打印检查

打印时：

1. 优先在 `Preview` 中打开 `A4 pdf`
2. 用 `100% scale` 或 `actual size`
3. 关闭 `fit to page`
4. 打完后用尺量黑色方码边长，确认真的是 `16 mm` 或 `12 mm`
5. 白边尽量完整保留，不要把黑码边缘裁掉

## 5. 当前建议结论

既然你说夹爪上已经有规定点，那第一轮就别绕了：

- 先打印 `16 mm`
- 先按规定点贴
- 如果发现规定点面积不够，再退到 `12 mm`

这样最接近 UMI 已验证过的路线，也最适合你现在这套 iPhone 安装视角。
