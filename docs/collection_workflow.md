# 采集流程

本文档描述当前这套 `UMIFT-datacollect` 的实际工作流。重点不是设备端实时联动，而是：

1. 把三路原始数据稳定录下来
2. 在笔记本端找到有效训练片段
3. 完成时间对齐和训练格式导出

## 适用硬件

- 全局相机：`Intel RealSense D435 / D435i`
- 夹爪相机：`iPhone 15 Pro`
- 力传感：`Teensy 4.1 + 2 x CoinFT`
- 主机：一台笔记本，`Mac` 优先，`Windows` 可兼容

说明：

- 当前你手里实际设备是 `D435i`
- 第一版把它按 `D435` 兼容相机使用
- IMU 数据先不采、也不参与对齐

## 采集前检查

### 1. 硬件连接

1. D435 通过 USB 连接笔记本
2. iPhone 通过有线连接笔记本
3. Teensy 4.1 通过 USB 连接笔记本
4. 左右 CoinFT 通过 UART 接到 Teensy

### 2. 基础健康检查

开始录制前，至少确认：

- D435 的 RGB / Depth 都能预览
- iPhone App 能显示 RGB / Depth / pose 状态
- Teensy 串口可见
- CoinFT 原始值在变化

这里要求：

- iPhone depth 必须真的在录
- D435 depth 也必须真的在录
- 即使第一版导出不带 depth，原始 run 里也不能缺 depth

### 3. 坐标系重置

操作者手持夹爪上的 iPhone 到一个自然起始姿态，按下：

```text
Reset Origin
```

此时建立本次 `session_world`：

- 手机当前位置作为原点
- 当时相机朝向作为该 session 的前向

如果后续需要固定偏移，可以在导出或对齐阶段叠加 `origin_offset_xyz_rpy`。

## 原始录制顺序

建议按下面顺序启动，不追求“同时开始”：

1. 启动 CoinFT 采集
2. 启动 D435 采集
3. iPhone 开始录制
4. 所有设备都进入持续录制状态

说明：

- 原始录制允许长一点
- 真正的训练片段通过后处理裁剪出来
- 这样可以避免某一路启动慢导致前几秒丢失
- CoinFT 左右两路都完整保存，不在采集阶段做 fusion

## 有效开始规则

开始信号不是按按钮，而是夹爪动作本身。

规则：

```text
第一次检测到夹爪接近闭合
-> 触发 3 秒倒计时
-> 倒计时结束
-> 有效片段开始
```

后处理公式：

```text
effective_start_time = first_close_time + 3.0
```

其中 `first_close_time` 来自 `gripper_width_by_frame.csv`。

### 为什么这样做

- 不依赖人手去卡录制按钮
- 避免最后时刻和开始时刻的人为抖动
- 让所有设备都可以提前进入稳定录制状态

## 有效结束规则

当前默认结束条件：

```text
在 effective_start_time 之后
iPhone 6DoF pose 连续 3 秒变化很小
-> 判定任务结束
```

建议阈值起点：

- `stable_window_s = 3.0`
- `translation_range_m < 0.01`
- `rotation_range_deg < 2.0`
- `min_episode_duration_s = 5.0`

### 这个规则的意义

- 不需要人去按停止按钮
- 避免人结束时按按钮造成最后一段异常位姿
- 如果操作者停住不动、忘了下一步，也可以自然结束

### 风险和补救

如果任务中途有“短暂停住观察”的动作，可能被误判为结束。出现这种情况时，优先加两层保护：

1. 提高 `min_episode_duration_s`
2. 叠加 gripper width 或 force 的稳定条件

第一版先用 pose 稳定窗口，保持实现简单。

## 单次 run 的目录结构

建议每次录制创建：

```text
runs/<run_id>/
├── RUN_METADATA.json
├── MANUAL_TRIM.json
├── global_camera/
├── iphone_gripper/
├── coinft/
├── gripper_width/
├── aligned/
└── export/
```

## 采集后处理顺序

说明：

- 当前 Mac 侧运行软件统一放在 `modules/04_laptop_alignment_export/`
- 这个目录既是工作区，也是 run 管理 / 数据同步 / 对齐导出的入口
- Mac GUI 功能规划见 `modules/04_laptop_alignment_export/mac_gui_plan.md`
- 数据契约与变换矩阵标准见 `modules/04_laptop_alignment_export/data_contract.md`

### 0. 初始化 run 并同步原始数据

推荐先在 Mac 上建立一次标准 run：

```bash
python modules/04_laptop_alignment_export/mac_sync_framework.py init-run <run_id>
```

然后把三路原始数据同步进去：

```bash
python modules/04_laptop_alignment_export/mac_sync_framework.py sync runs/<run_id> --module iphone_gripper --source <iphone_export_dir>
python modules/04_laptop_alignment_export/mac_sync_framework.py sync runs/<run_id> --module global_camera --source <global_camera_dump_dir>
python modules/04_laptop_alignment_export/mac_sync_framework.py sync runs/<run_id> --module coinft --source <coinft_dump_dir>
```

同步后先检查一次：

```bash
python modules/04_laptop_alignment_export/mac_sync_framework.py status runs/<run_id>
```

### 1. 拉取 iPhone 数据

把本次 demo 目录拉到：

```text
runs/<run_id>/iphone_gripper/
```

### 2. 检查原始数据完整性

至少确认：

- iPhone demo 存在，且 `poseTimes` 能解析
- iPhone RGB / Depth 文件都存在
- D435 视频和时间戳文件存在
- D435 Depth 文件存在
- CoinFT CSV 存在
- CoinFT 左右 CSV 都存在

### 3. 运行夹爪宽度估计

输出：

```text
runs/<run_id>/gripper_width/
├── gripper_width_by_frame.csv
├── gripper_width_debug.mp4
└── gripper_width_report.json
```

### 4. 运行时间轴对齐

推荐命令：

```bash
python tools/alignment/align_run_to_iphone.py runs/<run_id> --force-source left
```

如果后续想导出另一侧，再单独跑：

```bash
python tools/alignment/align_run_to_iphone.py runs/<run_id> --force-source right
```

当前约定是不使用 `fused` 作为第一版默认路径。

输出：

```text
runs/<run_id>/aligned/
├── aligned_index.csv
├── aligned_force.csv
├── aligned_pose.csv
├── ALIGNMENT_REPORT.json
└── ALIGNMENT_REPORT.md
```

### 5. 导出 ForceFlow Zarr

推荐命令：

```bash
python tools/export/export_forceflow_zarr.py runs/<run_id> --task <task_name>
```

导出前必须确认：

- 当前 `aligned/aligned_force.csv` 对应的是哪一侧
- 该侧信息已经写入 `RUN_METADATA.json` 或 `MANUAL_TRIM.json`

输出：

```text
runs/<run_id>/export/<task_name>.zarr/
```

## 第一版通过标准

满足下面条件，就算闭环跑通：

1. D435 原始 RGB / Depth 和时间戳文件存在
2. iPhone demo 存在，且 `poseTimes` / pose 可解析
3. CoinFT 左右 CSV 可解析
4. marker 宽度估计成功，能找到第一次接近闭合
5. 能得到 `effective_start_time`
6. 能得到 `effective_end_time`，或者报告需要人工裁剪
7. 对齐后每个 iPhone frame 都能找到对应的 D435 / CoinFT 数据
8. 能导出 `export/<task>.zarr/`
9. ForceFlow validator 可以通过

注意：

- `export/<task>.zarr/` 第一版不要求包含 depth
- 但原始 run 必须保留 iPhone / D435 的 depth

## 常见失败点

1. iPhone pose 字段名和当前脚本预期不一致
2. D435 时间戳列名不一致
3. CoinFT 时间范围和 iPhone 没有重叠
4. marker 遮挡导致夹爪宽度缺失
5. 自动结束把中途停顿误判成结束
6. iPhone 或 D435 虽然有 RGB，但 depth 实际没录到

## 实操建议

第一次采集时，建议不要一上来就录正式任务，先录三种短片：

1. `reset + 原地轻微移动`
2. `夹爪张开/闭合标定`
3. `完整短任务`

这样更容易把 pose、marker、CoinFT、导出链路一条条核实清楚。
