# 系统架构说明

## 一句话目标

在一台笔记本上集中采集、对齐并导出以下三路数据：

- 外置全局相机 `Intel RealSense D435 / D435i` 的 RGB / Depth
- 夹爪上的 `iPhone` 的 RGB / Depth / 6DoF 位姿
- `Teensy 4.1 + 2 x CoinFT` 的 6D 力/力矩原始数据

最终把它们整理成可直接供 ForceFlow 训练的 Zarr。

## 当前方案边界

这套项目已经不再把重点放在“iPhone 直接和外设无线同步”上，而是采用更快闭环的路线：

1. 所有设备都接到同一台笔记本。
2. 各自完整录制原始数据。
3. 以 iPhone 的 `poseTimes` 作为主时间轴。
4. 在笔记本端完成有效片段裁剪、时间对齐、视觉夹爪宽度估计、训练格式导出。

这样做的原因很现实：

- 可以最快拿到第一批可训练数据。
- 不需要先解决 ESP32 / 蓝牙 / 自动发现这一整套连接问题。
- 旧电脑也更容易先跑通“能采、能对齐、能导出”。

## 硬件分工

### 1. 外置全局相机

设备：`Intel RealSense D435 / D435i`

职责：

- 录制场景全局 RGB
- 录制场景全局 Depth
- 提供固定观察视角 `rgb_fix`

当前你手里是 `D435i`，第一版按 `D435` 兼容设备处理：

- 使用 RGB / Depth
- 暂不使用 IMU
- 文档里提到 `D435` 时，都默认包含 `D435i`

输出目录：

```text
runs/<run_id>/global_camera/
```

### 2. 夹爪相机

设备：`iPhone 15 Pro`

职责：

- 录制夹爪视角 RGB
- 录制夹爪视角 Depth
- 输出 ARKit / RealityKit 提供的 6DoF 位姿
- 在录制开始前执行 `Reset Origin`，建立本次 `session_world`

这里要特别写死一条：

```text
iPhone 采集阶段必须保存 depth
但第一版训练导出阶段不把 depth 写进 ForceFlow 必需字段
```

输出目录：

```text
runs/<run_id>/iphone_gripper/
```

### 3. 力传感链路

设备：`Teensy 4.1 + 2 x CoinFT`

职责：

- Teensy 通过 UART 读取左右 CoinFT 原始数据
- Teensy 通过 USB 串口把原始包转发到笔记本
- 笔记本端分别保存 `left` / `right` 两路数据
- 第一版不做左右 fusion
- 训练导出时显式选择 `left` 或 `right` 其中一路写入 `data/force`

输出目录：

```text
runs/<run_id>/coinft/
```

### 4. 笔记本本体

职责：

- 统一启动和保存各路采集
- 可视化健康状态检查
- 从 iPhone 视频中估计夹爪开合宽度
- 以 iPhone `poseTimes` 为主时间轴对齐 D435 / CoinFT
- 按训练格式导出 Zarr

另外它还负责：

- 保存 `RUN_METADATA.json`
- 保存人工复核和裁剪 override

输出目录：

```text
runs/<run_id>/
├── global_camera/
├── iphone_gripper/
├── coinft/
├── gripper_width/
├── aligned/
└── export/
```

## 设计原则

1. 先跑通真实数据闭环，再追求设备端实时同步。
2. 原始数据完整保留，后处理结果另存，不覆盖原始文件。
3. 第一版只保证 ForceFlow Zarr 导出，不同时追多种训练格式。
4. 不依赖加速度计；当前只使用 iPhone 的 6DoF 位姿。
5. 不强求第一版完成跨传感器空间融合，先完成时间对齐。

## 主时间轴

当前统一规定：

```text
target_t[i] = iphone.poseTimes[i]
```

在这个主时间轴下：

- `rgb_arm` 来自 iPhone 对应帧
- `rgb_fix` 来自 D435 最近邻帧
- `pos` 来自 iPhone 在 `session_world` 中的 6DoF 位姿
- `force` 来自 CoinFT 单侧对齐后的 6D wrench
- `gripper_state` 来自视觉夹爪宽度阈值化

## 有效片段定义

第一版不要求所有设备实时同步开始和结束，而是先完整录制，再在笔记本端裁剪有效训练片段。

### 开始规则

```text
第一次检测到夹爪接近闭合
-> 触发 3 秒倒计时
-> 倒计时结束
-> effective_start_time
```

公式：

```text
effective_start_time = first_close_time + 3.0
```

其中 `first_close_time` 来自 iPhone 视频上的 marker 宽度估计。

### 结束规则

当前采用自动结束：

```text
在 effective_start_time 之后
iPhone 6DoF pose 连续 3 秒抖动很小
-> 判定任务结束
-> effective_end_time
```

并加保护条件：

```text
effective_end_time >= effective_start_time + min_episode_duration_s
```

第一版建议：

- `stable_window_s = 3.0`
- `min_episode_duration_s = 5.0`
- 若误判偏多，再叠加 gripper / force 稳定条件

## 坐标系定义

### iPhone `session_world`

录制前在 iPhone App 上执行 `Reset Origin`：

```text
T_world_phone_at_reset = Identity
```

之后每帧位姿：

```text
T_world_phone(t) = inverse(T_arkit_phone_at_reset) * T_arkit_phone(t)
```

这意味着：

- 按下 `Reset Origin` 的那一刻，手机位置是原点
- 当时相机朝向定义为这次录制的正方向
- 允许后续额外加一个固定 `origin_offset_xyz_rpy`，用于人工微调

### 其它坐标系

当前阶段只要求：

- iPhone pose 使用 `session_world`
- CoinFT 先保留在 `coinft frame`
- D435 只做时间对齐，不要求先解空间外参

如果后续要做统一空间坐标系，再补：

- iPhone 内参
- D435 内参
- D435 到 `session_world` 外参
- 夹爪 marker / 手眼标定

## 训练导出目标

当前固定导出 ForceFlow Zarr：

```text
export/<task>.zarr/
├── data/rgb_arm
├── data/rgb_fix
├── data/pos
├── data/force
├── data/action
├── data/gripper_state
├── data/gripper_action
├── data/episode
├── meta/episode_ends
└── <task>_normalizer.json
```

具体字段要求见：

- `docs/training_zarr_alignment.md`
- `docs/training_format_implementation.md`
- `schemas/forceflow_zarr_schema.md`

补充约束：

- `depth_arm` 和 `depth_fix` 在 run 内必须保留
- 第一版 `export/<task>.zarr/` 不强制写 depth
- `data/force` 第一版只接受单侧 `left` 或 `right`，不使用 fused

## 推荐实现分层

### 采集层

- `tools/capture/` 负责 D435 / Teensy / iPhone 原始数据落盘

### 估计层

- `tools/gripper_width/` 负责 marker 宽度估计

### 对齐层

- `tools/alignment/align_run_to_iphone.py`

### 导出层

- `tools/export/export_forceflow_zarr.py`
- `docs/run_metadata_schema.md`
- `docs/manual_review_and_trim.md`

## Mac / Windows 角色

这套方案本质上是“笔记本集中式”，不是“只限 Mac”。

但当前建议仍然是：

- `Mac`：优先承担 iPhone App 安装、容器导出、集中对齐和导出
- `Windows`：更适合 D435 / Teensy 采集稳定性测试

如果后续要做跨平台正式版，可以拆成：

1. Windows 负责采集
2. Mac 负责拉取 iPhone 数据
3. 任意一台机器负责对齐和导出

第一版先不把系统拆太散。
