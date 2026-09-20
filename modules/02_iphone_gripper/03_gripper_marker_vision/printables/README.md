# 可直接打印的夹爪 finger marker

## 1. 现在推荐直接打印什么

优先直接打印整页 `A4` 拼版：

- `a4_sheet_mixed_12mm_16mm_300dpi.pdf`

如果你想只打一种尺寸：

- `a4_sheet_id0_id1_16mm_300dpi.pdf`
- `a4_sheet_id0_id1_12mm_300dpi.pdf`

同名 `png` 也会一起生成，但实际打印时更建议直接开 `pdf`。

单张 cutout 仍然保留：

- `aruco_left_id0_16mm.png`
- `aruco_right_id1_16mm.png`
- `aruco_left_id0_12mm.png`
- `aruco_right_id1_12mm.png`

注意：上面这些单张 cutout PNG 底部带有文字标签，不是纯正方形 marker。如果要把单张图片直接按边长贴到夹爪上，应该使用裁剪后的纯 marker：

- `aruco_left_id0_16mm_marker_only.png`
- `aruco_right_id1_16mm_marker_only.png`

原始 `16mm` PNG 尺寸是 `331x387`，纯 marker 区域是 `189x189`。不要把整张带文字 PNG 缩放成 `16 mm`，否则真正方码会小于目标尺寸。

参数固定为：

- 字典：`DICT_4X4_50`
- 左夹爪 id：`0`
- 右夹爪 id：`1`
- 黑码边长：主推荐 `16 mm`，备选 `12 mm`

## 2. 为什么是这两个

原因有两个：

1. 这和 UMI finger marker 的公开配置一致
2. 对当前 iPhone 夹爪开合度估计，第一版只需要左右 finger marker，不需要整套大 marker 组

## 3. 怎么打印

打印时注意：

1. 优先在 `Preview` 里打开 `A4 pdf`
2. 按 `100% scale / actual size`
3. 不要 `fit to page`
4. 用普通白底激光或喷墨都可以，但必须保证黑白对比强
5. 打印后用尺量黑色方码边长，确认实际尺寸是 `16 mm` 或 `12 mm`
6. 尽量覆到平整底材上，再贴到夹爪规定点

## 4. 怎么贴

- 左夹爪贴 `id 0`
- 右夹爪贴 `id 1`
- 优先贴到夹爪现有 marker 规定点
- 如果规定点不是正对相机的平面，考虑加一个很小的平片作为载体

## 5. 附带文件

- `aruco_finger_config.yaml`
- `aruco_pair_id0_id1_16mm_300dpi.png`
- `aruco_pair_id0_id1_12mm_300dpi.png`
- `a4_sheet_id0_id1_16mm_300dpi.pdf`
- `a4_sheet_id0_id1_12mm_300dpi.pdf`
- `a4_sheet_mixed_12mm_16mm_300dpi.pdf`

双 marker 同页版本适合单独裁切，`A4` 拼版适合直接拿去打印
