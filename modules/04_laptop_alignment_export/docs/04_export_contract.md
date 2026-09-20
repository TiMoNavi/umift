# UMI-FT Zarr 导出契约

> 当前导出契约以本文为准。历史模块拆分讨论已从交付包中移除。

本文档替代之前围绕 ForceFlow 扁平 Zarr 的导出方案。当前主目标是按 `external UMI-FT source tree` 原项目逻辑输出 UMI-FT replay buffer Zarr。

核心结论：

- 不再把 iPhone SLAM pose 强行解释成机器人绝对 EEF pose。
- 当前采到的 SLAM 轨迹可以作为 UMI-FT 的 trajectory state 使用。
- 导出格式应从 ForceFlow 的 `data/pos/action/force` 改为 UMI-FT 的 `data/episode_xxx/...` replay buffer 结构。
- 训练时 UMI-FT dataset 会把 pose 转成相对当前 pose 的表示，因此不需要机器人 base 下的绝对原点。

重要边界：

- 本文先定义 UMI-FT 目标占位契约，不把当前 receiver 已经收到的字段当成最终标准。
- 当前 receiver/app 的字段只是 V0 数据来源；后续应逐步改成直接产出更接近 UMI-FT exporter 需要的标准中间数据。
- D435 全局相机属于笔记本端全局视角逻辑，和 iPhone gripper camera 并列进入 exporter；不要让 iPhone receiver 直接承担 D435 字段语义。

## 1. 教授要求的真实含义

教授说“按照 UMI-FT 原本逻辑输出”，这里不是要求我们补采机器人真实 EEF pose，而是要求：

```text
用 SLAM 走过的轨迹作为 state/action trajectory，
按 UMI-FT replay buffer 格式保存，
让 UMI-FT dataset 在训练采样时做相对 pose 变换。
```

UMI-FT 原项目里字段虽然叫 `robot0_eef_pos`，但 raw Zarr 里真正保存的是：

```text
ts_pose_fb_0: [x, y, z, qw, qx, qy, qz]
```

训练读取时才转成：

```text
robot0_eef_pos
robot0_eef_rot_axis_angle
```

这个命名是训练接口命名，不代表数据必须来自机器人控制器的绝对 EEF pose。

## 2. UMI-FT 目标格式

最终训练用 Zarr 是 episode 分组结构：

```text
acp_replay_buffer_gripper.zarr/
  data/
    episode_0/
      rgb_0
      rgb_time_stamps_0
      rgb_global_0
      rgb_global_time_stamps_0
      depth_0
      depth_time_stamps_0
      map_to_d_idx_0
      ts_pose_fb_0
      robot_time_stamps_0
      gripper_0
      gripper_time_stamps_0
      wrench_left_0
      wrench_right_0
      wrench_concat_0
      wrench_left_coinft_0
      wrench_right_coinft_0
      wrench_concat_coinft_0
      wrench_time_stamps_left_0
      wrench_time_stamps_right_0
      wrench_time_stamps_0
      ts_pose_command_0
      ts_pose_virtual_target_0
      stiffness_0
  meta/
    episode_rgb0_len
    episode_robot0_len
    episode_gripper0_len
    episode_wrench0_len
    episode_depth0_len
    episode_rgb_global0_len
```

UMI-FT 原项目可选字段还包括：

```text
ultrawide_0
ultrawide_time_stamps_0
map_to_uw_idx_0
```

当前 V0 已经把 D435 全局相机写成两个字段：

```text
rgb_global_0: D435/global RGB，和 rgb_0 使用同一主时间轴
depth_0: D435/global depth，按 UMI-FT 现有 depth 字段写入
```

`rgb_global_0` 是我们新增的扩展字段；UMI-FT 原训练配置不会自动使用它，后续需要在 shape_meta 里显式加入。`depth_0` 是 UMI-FT 已经认识的字段。

## 3. 最小 V0 字段要求

第一版只要能训练/读取核心链路，建议导出：

| UMI-FT 字段                    | shape                | dtype       | 来源                               | 说明                                                                                                 |
| ------------------------------ | -------------------- | ----------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `rgb_0`                      | `(T, 224, 224, 3)` | `uint8`   | iPhone RGB                         | UMI-FT 是 THWC，不是 CHW。                                                                           |
| `rgb_time_stamps_0`          | `(T, 1)`           | `float64` | iPhone frame time                  | 秒，建议 episode-local，从 0 开始。                                                                  |
| `rgb_global_0`               | `(T, 224, 224, 3)` | `uint8`   | D435 RGB                           | 新增全局相机扩展字段；按`rgb_0` 主时间轴对齐。                                                     |
| `rgb_global_time_stamps_0`   | `(T, 1)`           | `float64` | D435 frame time                    | 秒，episode-local。                                                                                  |
| `depth_0`                    | `(T, 224, 224, 3)` | `float16` | D435 depth                         | 米，clip 到`0.5m`，重复为 3 通道以兼容 UMI-FT depth 预处理。                                       |
| `depth_time_stamps_0`        | `(T, 1)`           | `float64` | D435 depth time                    | 秒，episode-local。                                                                                  |
| `map_to_d_idx_0`             | `(T, 1)`           | `int64`   | exporter                           | 当前 D435 depth 已按主时间轴写入，所以是 identity index。                                            |
| `ts_pose_fb_0`               | `(T, 7)`           | `float64` | iPhone SLAM pose + UMI-FT 外参转换 | `[x,y,z,qw,qx,qy,qz]`；导出端保持 world origin 不变，把 phone pose 转成原 UMI-FT GoPro/tool 口径。 |
| `robot_time_stamps_0`        | `(T, 1)`           | `float64` | pose sample time                   | 与`ts_pose_fb_0` 对齐，秒。                                                                        |
| `gripper_0`                  | `(T, 1)`           | `float64` | iPhone gripper                     | 默认写米制宽度；若 app 未给`width_m`，导出端用 open fraction 线性映射。                            |
| `gripper_time_stamps_0`      | `(T, 1)`           | `float64` | gripper sample time                | 秒。                                                                                                 |
| `wrench_left_coinft_0`       | `(F, 6)`           | `float64` | 左 CoinFT 模型输出                 | CoinFT 原始/传感器坐标下的 6D wrench。                                                               |
| `wrench_right_coinft_0`      | `(F, 6)`           | `float64` | 右 CoinFT 模型输出                 | 同上。                                                                                               |
| `wrench_concat_coinft_0`     | `(F, 12)`          | `float64` | 左右拼接                           | `[left6, right6]`。                                                                                |
| `wrench_left_0`              | `(F, 6)`           | `float64` | exporter                           | 用原 UMI-FT CoinFT->TCP/tool 几何从`wrench_left_coinft_0` 转换。                                   |
| `wrench_right_0`             | `(F, 6)`           | `float64` | exporter                           | 用原 UMI-FT CoinFT->TCP/tool 几何从`wrench_right_coinft_0` 转换。                                  |
| `wrench_concat_0`            | `(F, 12)`          | `float64` | 左右 TCP/tool wrench 拼接          | `[left6, right6]`；UMI-FT 训练配置通常读这个或左右两路。                                           |
| `wrench_time_stamps_left_0`  | `(F, 1)`           | `float64` | 左 CoinFT time                     | 秒。                                                                                                 |
| `wrench_time_stamps_right_0` | `(F, 1)`           | `float64` | 右 CoinFT time                     | 秒。                                                                                                 |
| `wrench_time_stamps_0`       | `(F, 1)`           | `float64` | 左右平均或主接收时间               | 供 UMI-FT sampler 对齐 wrench。                                                                      |

`T` 是相机/pose/gripper 主时间轴长度，`F` 是 CoinFT 高频采样长度。二者不需要相同，UMI-FT 原逻辑就是用 timestamp 对齐。

## 4. 哪些只是格式问题

这些不需要重采，改 exporter 即可：

1. Zarr 结构

   从 ForceFlow 扁平结构：

   ```text
   data/pos
   data/action
   data/force
   ```

   改成 UMI-FT episode 结构：

   ```text
   data/episode_0/ts_pose_fb_0
   data/episode_0/rgb_0
   data/episode_0/gripper_0
   data/episode_0/wrench_left_0
   data/episode_0/wrench_right_0
   meta/episode_robot0_len
   meta/episode_rgb0_len
   meta/episode_wrench0_len
   ```
2. 图像 shape

   ForceFlow 之前使用：

   ```text
   (T, 3, 240, 320)
   ```

   UMI-FT 需要：

   ```text
   (T, 224, 224, 3)
   ```
3. pose 表达

   ForceFlow 之前使用：

   ```text
   [x, y, z, roll, pitch, yaw]
   ```

   UMI-FT 需要：

   ```text
   [x, y, z, qw, qx, qy, qz]
   ```
4. force 字段命名

   之前双路 force 是：

   ```text
   data/force: (T, 2, 6)
   ```

   UMI-FT 应保存为：

   ```text
   wrench_left_0
   wrench_right_0
   wrench_concat_0
   wrench_left_coinft_0
   wrench_right_coinft_0
   wrench_concat_coinft_0
   ```
5. timestamp

   UMI-FT 需要每路自己的 timestamp array，而不是所有字段强行对齐成同一个 `N`。

## 5. 哪些是语义口径要改

### 5.1 不再说这是绝对 EEF pose

当前应写：

```text
ts_pose_fb_0 = iPhone/user_world SLAM trajectory transformed at export time to original UMI-FT GoPro/tool convention
```

不要写：

```text
ts_pose_fb_0 = robot absolute EEF pose
```

### 5.2 UMI-FT 会做相对 pose

UMI-FT dataset 读取 `ts_pose_fb_0` 后，会先转成 SE(3)，再在采样时计算相对当前 pose：

```text
relative_pose = inv(current_pose) @ sampled_pose
```

所以训练并不要求 world origin 等于 robot base origin。

### 5.3 坐标系仍然要稳定

不需要 robot base 绝对原点，不代表坐标系可以乱。

第一版允许：

```text
pose_frame = iphone_user_world_phone
```

但必须在 manifest 里声明：

```text
pose_source = iPhone ARKit/user_world SLAM
pose_object = phone/camera rigidly mounted on gripper
is_robot_absolute_eef = false
translation_unit = meter
quaternion_order = qw,qx,qy,qz
```

如果要更接近 UMI-FT 原始 GoPro/TCP frame，可以增加固定外参：

```text
T_world_tool = T_world_phone @ T_phone_tool
```

这仍然不是 robot base absolute EEF，只是把手机 pose 换到稳定工具/相机 frame。

## 6. 哪些目前没直接采到

这些需要写清楚，但不一定阻塞第一版：

| 项目                              | 是否采到                      | 是否阻塞 UMI-FT V0 | 处理方式                                                                                                        |
| --------------------------------- | ----------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------- |
| 机器人真实绝对 EEF pose           | 没采到                        | 不阻塞             | UMI-FT 原逻辑可用 SLAM trajectory state。                                                                       |
| 机器人真实 command                | 没采到                        | 不阻塞             | V0 设置`ts_pose_command_0 = ts_pose_fb_0`。                                                                   |
| `ts_pose_virtual_target_0`      | 不是直接采集                  | 训练版需要         | 后处理根据 wrench 和`ts_pose_fb_0` 生成；V0 可先占位或先输出 raw replay buffer。                              |
| `stiffness_0`                   | 不是直接采集                  | 训练版需要         | 后处理生成；V0 可先占位常数并标 manifest。                                                                      |
| CoinFT 到 tool/TCP frame 的力变换 | exporter 已套用原 UMI-FT 常量 | 不阻塞 V0          | 采集端仍保存 CoinFT calibrated 输出；导出端生成`_coinft_0` 原坐标和 `wrench_left_0/right_0` TCP/tool 坐标。 |
| D435 作为全局相机                 | 已接入 V0                     | 不阻塞 V0          | 当前写`rgb_global_0` 扩展字段和 `depth_0` 标准字段；训练是否使用由 shape_meta 决定。                        |

## 7. 插件式导出架构要求

为了防止训练格式再次变化时反复改主链路，最终 Zarr 导出必须做成插件式模块，而不是写死在 GUI、receiver 或主 recording pipeline 中。

稳定核心只负责：

```text
raw capture
recording windows / episodes
normalized streams
aligned timeline or per-stream timestamped data
standard intermediate rows / stream views
```

易变化的部分放进 exporter plugin：

```text
Zarr group layout
field names
image shape / layout
pose representation
force representation
action / virtual target generation
timestamp convention
manifest schema
validation rules
```

### 7.1 推荐目录结构

建议在 `umift_laptop_alignment/pipeline/export/` 下形成：

```text
pipeline/export/
  base.py
  controller.py
  registry.py
  debug_jsonl/
    exporter.py
  index_csv/
    exporter.py
  umift_replay_buffer/
    exporter.py
    schema.py
    defaults.yaml
  forceflow_legacy/
    exporter.py
```

其中：

- `base.py` 定义 exporter interface。
- `controller.py` 维护主链路当前选择的 export format/profile 和运行状态。
- `registry.py` 负责按 `format` 找 exporter。
- `umift_replay_buffer/` 是当前第一个正式训练导出插件。
- `forceflow_legacy/` 如需保留，只作为历史兼容插件，不得成为主线依赖。

### 7.2 Exporter interface

每个 exporter plugin 至少声明：

```text
name
version
description
required_inputs
default_config
export(run_dir, output_dir, config)
validate(output_dir)
manifest_schema
```

推荐伪代码：

```python
class ExporterPlugin:
    name: str
    version: str

    def default_config(self) -> dict:
        ...

    def required_inputs(self) -> list[str]:
        ...

    def export(self, *, run_dir, output_dir, config) -> dict:
        ...

    def validate(self, *, output_dir, config) -> dict:
        ...
```

主流程只调用：

```text
ExporterRegistry.get(format).export(run_dir, output_dir, config)
```

主流程不直接知道：

```text
ts_pose_fb_0
rgb_0
wrench_left_0
data/pos
data/action
```

这些都是插件内部 schema。

### 7.3 配置文件驱动

GUI 不应该上传模型或硬编码字段规则。GUI 只选择一个本地 export config，然后触发导出。

UMI-FT exporter 的配置建议：

```yaml
export:
  format: umift-replay-buffer-zarr
  output_name: acp_replay_buffer_gripper.zarr

image:
  rgb_0:
    source: iphone
    size: [224, 224]
    layout: THWC
  global_camera:
    enabled: true
    source: d435
    write_rgb: true
    rgb_field: rgb_global_0
    write_depth_0: true
    depth_clip_m: 0.5
    size: [224, 224]
    layout: THWC

pose:
  reference_frame: user_world
  source_frame: iphone_camera
  target_frame: gripper_center_tcp
  representation: pose7_qwxyz
  euler_input_convention: roll_pitch_yaw_degrees__Rz_yaw_Ry_pitch_Rx_roll
  translation_unit: m
  transform: iphone_camera_to_gripper_center_tcp
  iphone_camera_t_gripper_center_tcp:
    - [1.0, 0.0, 0.0, 0.039711]
    - [0.0, -1.0, 0.0, -0.094423]
    - [0.0, 0.0, -1.0, -0.257957]
    - [0.0, 0.0, 0.0, 1.0]
  is_robot_base_absolute: false

timestamps:
  unit: seconds
  origin: episode_start_aligned_monotonic_ns

gripper:
  source: iphone_open_percent
  output: width_m_from_open_fraction_linear
  fallback_min_width_m: 0.0
  fallback_max_width_m: 0.08

force:
  source: coinft_dual_calibrated
  write_coinft_passthrough: true
  write_tool_frame: umi_coinft_to_tcp
  write_concat: true
  units: [N, N, N, Nm, Nm, Nm]
  coinft_to_tcp:
    dist_coinft2gopro_along_cam_z_m: 0.166
    dist_coinft2gopro_along_cam_y_m: 0.081
    gripper_width_source: gripper_width_m_or_open_fraction_linear

actions:
  ts_pose_command_0: copy_ts_pose_fb_0
  virtual_target: placeholder_equal_pose_fb
  stiffness: placeholder_constant
  stiffness_constant: 0.0
```

配置文件建议放在：

```text
modules/04_laptop_alignment_export/config/export_profiles/
  umift_replay_buffer_v0.json
```

run 输出时，必须把实际使用的 config 复制到 export 目录：

```text
<run_dir>/exports/umift_replay_buffer_zarr/
  EXPORT_CONFIG.json
  UMIFT_EXPORT_MANIFEST.json
  acp_replay_buffer_gripper.zarr/
```

### 7.4 GUI 边界

GUI 只做：

```text
显示 available exporter formats
选择 export profile
更新主链路 export_control state
触发 export command
显示 exporter status / validation result
打开 output folder
```

GUI 不做：

```text
拼 Zarr 字段
决定 pose 表达
决定 force 字段 shape
写 manifest
直接调用 receiver 内部数据结构
```

主链路状态必须有单独的导出控制字段：

```text
/state.state.export_control
  selected_format
  selected_profile
  available_formats
  available_profiles
  status
  last_manifest
  last_error
  updated_unix_ns
```

GUI 通过 `/main/command` 只调用两类命令：

```text
configure_export:
  只修改 selected_format / selected_profile
  不执行任何 Zarr 转换

export_selected:
  读取当前 export_control
  调用 ExportController
  再由 ExporterRegistry 找到具体 exporter plugin
```

这样 GUI 可以引用和更新导出格式状态，但不需要知道 `rgb_0`、`ts_pose_fb_0`、`wrench_concat_0` 等 schema 细节。

### 7.5 为什么现在就要做

当前已经发生过一次主目标变化：

```text
ForceFlow flat zarr -> UMI-FT replay buffer zarr
```

后续仍可能出现：

```text
UMI-FT V0 raw replay buffer
UMI-FT training-ready replay buffer
UMI-FT with depth/ultrawide
UMI-FT with tool-frame wrench
legacy ForceFlow export
debug inspection export
```

这些变化不应该影响采集层和主 GUI。因此 UMI-FT exporter 应作为第一个正式插件实现，而不是直接写成唯一硬编码 exporter。

## 8. 当前实现状态与推荐路径

### 已完成: exporter plugin 骨架

当前已经建立：

```text
pipeline/export/base.py
pipeline/export/controller.py
pipeline/export/registry.py
pipeline/export/umift_replay_buffer/
```

并让 CLI 入口支持：

```bash
python3 entrypoints/export_run.py <run_dir> <source_episode_index> \
  --format umift-replay-buffer-zarr \
  --config config/export_profiles/umift_replay_buffer_v0.json
```

主 GUI 入口也已经接入同一个 controller：

```text
receiver_web_gui.py
  state.export_control
  /main/command configure_export
  /main/command export_selected
  Export format / Export profile / Export Selected controls
```

默认选择：

```text
format: umift-replay-buffer-zarr
profile: config/export_profiles/umift_replay_buffer_v0.json
```

### 已完成: 最小 UMI-FT exporter

当前导出格式：

```bash
python3 entrypoints/export_run.py <run_dir> <source_episode_index> --format umift-replay-buffer-zarr
```

输出目录建议：

```text
<run_dir>/exports/umift_replay_buffer_zarr/acp_replay_buffer_gripper.zarr
```

当前实现按 `aligned/episodes/episode_XXXXXX/` 写单个 source Episode。D435 元数据中的 `episodeIndex`/开始时间会匹配同一 iPhone session 内唯一的 `record_start -> record_stop` 窗口；临时 group 完成并通过质量门控后才原子提交到连续的 `data/episode_N`，不会重建已有 Episode。

### 已完成: 写 raw episode fields

从当前 normalized/aligned/raw 文件生成：

```text
data/episode_i/rgb_0
data/episode_i/rgb_time_stamps_0
data/episode_i/ts_pose_fb_0
data/episode_i/robot_time_stamps_0
data/episode_i/gripper_0
data/episode_i/gripper_time_stamps_0
data/episode_i/wrench_*
data/episode_i/rgb_global_0
data/episode_i/depth_0
meta/episode_*_len
```

注意：

- `rgb_0` 用 iPhone RGB。
- `ts_pose_fb_0` 用 iPhone `user_world` pose 套原 UMI-FT iPhone15 Pro 外参后转 pose7 quaternion。
- `wrench_*_coinft_0` 保留 CoinFT calibrated dual force；`wrench_left/right_0` 写导出端 TCP/tool-frame 转换结果。
- `rgb_global_0` / `depth_0` 用 D435 全局相机。
- timestamp 用 float64 秒，episode-local。

### 下一步: 补 training-ready action fields

若要直接接 UMI-FT 训练配置，需要补：

```text
ts_pose_command_0
ts_pose_virtual_target_0
stiffness_0
```

第一版规则：

```text
ts_pose_command_0 = ts_pose_fb_0
stiffness_0 = constant or postprocess estimate
ts_pose_virtual_target_0 = ts_pose_fb_0 initially, then replace by force-based postprocess
```

只要使用占位，就必须在 manifest 中标注：

```text
virtual_target_mode = placeholder_equal_pose_fb
stiffness_mode = placeholder_constant
```

### 下一步: 对接 UMI-FT Dataset

验证目标：

```text
UmiFTDataset 能打开 acp_replay_buffer_gripper.zarr
raw_to_obs 能读 ts_pose_fb_0 / rgb_0 / wrench_left_0 / wrench_right_0
sampler 能生成一个 batch
pose 在 sampler 中成功转为 relative pose
```

当前已验证：

```text
UMI-FT diffusion_policy.common.replay_buffer.ReplayBuffer.copy_from_store
可以打开本 exporter 生成的 acp_replay_buffer_gripper.zarr。
```

## 9. 推荐 manifest

UMI-FT 原始 Zarr 不一定有 manifest，但我们自己的导出建议加：

```text
UMIFT_EXPORT_MANIFEST.json
```

至少写：

```json
{
  "format": "umift-replay-buffer-zarr",
  "pose_reference_frame": "user_world",
  "pose_source_frame": "iphone_camera",
  "pose_target_frame": "gripper_center_tcp",
  "pose_transform": "iphone_camera_to_gripper_center_tcp",
  "is_robot_base_absolute": false,
  "pose_array": "ts_pose_fb_0",
  "pose7_order": ["x", "y", "z", "qw", "qx", "qy", "qz"],
  "translation_unit": "m",
  "timestamp_unit": "s",
  "timestamp_origin": "episode_start",
  "force_source": "dual_coinft_calibrated",
  "force_side_order": ["left", "right"],
  "force_axes": ["fx", "fy", "fz", "mx", "my", "mz"],
  "wrench_frame": "gripper_center_tcp",
  "command_mode": "ts_pose_command_0_equals_ts_pose_fb_0",
  "virtual_target_mode": "placeholder_or_postprocess",
  "stiffness_mode": "placeholder_or_postprocess"
}
```

## 10. 当前不再作为主线的内容

以下旧方向已从主文档中移除：

- ForceFlow `data/pos/action/force` 扁平 Zarr 作为主导出目标。
- 把 iPhone pose 转成 robot base absolute EEF pose 才能训练的假设。
- `data/action = pos[t+1] - pos[t]` 作为当前主导出 action 的假设。
- ForceFlow handoff Zarr 样例包。

如果以后仍要兼容 ForceFlow，可以另开 `legacy_forceflow_export.md`，不要和当前 UMI-FT 主线混在一起。

## 11. 完成标准

第一阶段完成标准：

- docs 中主方案只指向 UMI-FT Zarr。
- exporter 走 plugin registry，不由 GUI 或主链路硬编码 Zarr schema。
- UMI-FT exporter 有独立 config，并把实际使用的 `EXPORT_CONFIG.json` 复制进输出目录。
- exporter 可以输出 episode 分组 Zarr。
- `ts_pose_fb_0` 是 pose7 quaternion。
- 图像是 THWC `224x224`。
- 双路 CoinFT 是 `wrench_left/right/concat`。
- 每路 timestamp 存在且单位明确。
- manifest 明确写 `is_robot_absolute_eef=false`。

第二阶段完成标准：

- 能用 `external UMI-FT source tree` 的 `UmiFTDataset` 读取一个 batch。
- 若训练配置需要 `ts_pose_virtual_target_0` 和 `stiffness_0`，已由后处理生成或 manifest 明确为占位。

## 12. UMI-FT 占位契约与采集端改造

这一节定义“我们想要的 UMI-FT 标准输入”，暂时不以当前 receiver/app 已经传了什么为限制。当前 exporter 可以继续兼容现有 normalized/aligned 文件，但后续 receiver 和 iPhone app 应向这里靠拢。

目标数据流：

```text
iPhone app
  -> iPhone receiver raw packet log
  -> iPhone normalized source view
  -> alignment timeline
  -> UMI-FT exporter

D435 global camera receiver
  -> D435 normalized source view
  -> alignment timeline
  -> UMI-FT exporter

CoinFT receiver
  -> calibrated dual wrench normalized source view
  -> alignment timeline / high-rate force stream
  -> UMI-FT exporter
```

### 12.1 UMI-FT exporter 的目标占位字段

第一版 exporter 应把最终训练 Zarr 看成这些逻辑输入，而不是直接绑定某个 app JSON 字段名：

| 逻辑输入                | 目标 Zarr 字段               | 来源职责                                | 当前占位策略                                                                              |
| ----------------------- | ---------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------- |
| gripper-mounted RGB     | `rgb_0`                    | iPhone app / iPhone receiver            | 可由当前 iPhone JPEG 帧生成。                                                             |
| gripper pose trajectory | `ts_pose_fb_0`             | iPhone app / iPhone receiver + exporter | exporter 计算 `T_user_world_gripper_center_tcp = T_user_world_iphone_camera @ T_iphone_camera_gripper_center_tcp`，不把首帧归零。 |
| gripper opening         | `gripper_0`                | iPhone app gripper vision + exporter    | 默认写米制宽度；优先 app`width_m`，否则 open fraction 线性映射。                        |
| global RGB              | `rgb_global_0`             | D435 receiver                           | 按 iPhone 主时间轴选最近 D435 RGB。                                                       |
| global depth            | `depth_0`                  | D435 receiver                           | 按 iPhone 主时间轴选最近 D435 depth，写 UMI-FT 标准 depth 字段。                          |
| left/right wrench       | `wrench_left_0/right_0`    | CoinFT receiver + exporter              | `_coinft_0` 保留 sensor-frame；非 `_coinft` 字段写原 UMI-FT TCP/tool-frame 转换结果。 |
| command pose            | `ts_pose_command_0`        | 后处理或未来 app/robot command stream   | V0 占位为`ts_pose_fb_0`。                                                               |
| virtual target          | `ts_pose_virtual_target_0` | UMI-FT 后处理                           | V0 占位为`ts_pose_fb_0`，后处理可覆盖。                                                 |
| stiffness               | `stiffness_0`              | UMI-FT 后处理                           | V0 占位常数，后处理可覆盖。                                                               |

### 12.2 iPhone app 需要改造什么

当前重点 app 是：

```text
modules/02_iphone_gripper/ios_app/UMIFTiPhoneCaptureCore/
```

后续建议新增 `RecordingTransmitPacketV2`，不要在 V1 字段上继续堆临时含义。V2 目标是让 app 直接发送 exporter 需要的 iPhone 标准源数据。

需要改造的文件与职责：

| 文件                                                                                | 需要改造                                                                                                                                                       |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `App/Recording/RecordingModeModels.swift`                                         | 定义`RecordingTransmitPacketV2`。新增 pose7 quaternion、完整 4x4 transform、camera intrinsics、gripper width/open fraction、calibration ids、quality flags。 |
| `App/Recording/RecordingFrameBuffer.swift`                                        | 保存 ARKit`cameraInUserWorldTransform` 的原始矩阵和 quaternion，不只保存 Euler。输出 `pose7_qwxyz`。                                                       |
| `App/Recording/RecordingTCPJSONLServer.swift`                                     | 支持发送 V2 packet。保留 V1 兼容，但 receiver 应能根据`schema_version` 分流。                                                                                |
| `App/Recording/RecordingLocalJSONLWriter.swift`                                   | 本地 JSONL 也写同一套 V2 字段，方便离线排查 app 与 receiver 差异。                                                                                             |
| `App/ARCapture/ARCaptureModel.swift`                                              | 把 ARFrame timestamp、RGB、intrinsics、pose、tracking state、world calibration state 传入 V2 packet builder。                                                  |
| `App/Calibration/WorldCalibrationARView.swift`                                    | 输出稳定的`world_origin_id`、user-world 定义、calibration version；不要只把标定结果用于 UI 文本。                                                            |
| `App/GripperVision/GripperVisionModels.swift` 和 `GripperVisionProcessor.swift` | 输出`open_fraction_0_1`，并尽量输出 `gripper_width_m`、confidence、marker ids、calibration id。                                                            |

建议 iPhone V2 packet 至少包含：

```json
{
  "schema_version": 2,
  "packet_type": "iphone_gripper_frame",
  "session_id": "iphone_YYYYMMDD_HHMMSS",
  "sequence": 12345,
  "phone_capture_monotonic_ns": 123456789,
  "phone_capture_unix_ns": 1780000000000000000,
  "timebase_status": "receiver_aligned",
  "pose": {
    "valid": true,
    "frame": "iphone_user_world",
    "world_calibrated": true,
    "world_origin_id": "mark_000001",
    "tracking": "normal",
    "position_m": [0.0, 0.0, 0.0],
    "quaternion_qwxyz": [1.0, 0.0, 0.0, 0.0],
    "camera_in_user_world_4x4": []
  },
  "rgb": {
    "role": "gripper_rgb",
    "width": 1920,
    "height": 1440,
    "pixel_format": "jpeg",
    "payload_ref": "chunk-or-base64",
    "intrinsics": {
      "fx": 0.0,
      "fy": 0.0,
      "cx": 0.0,
      "cy": 0.0
    }
  },
  "gripper": {
    "valid": true,
    "open_fraction_0_1": 1.0,
    "width_m": null,
    "confidence": 1.0,
    "calibration_id": "gripper_path_000001"
  },
  "recording": {
    "is_recording": true,
    "event": null,
    "episode_index": 0
  },
  "quality": {
    "dropped_frame_count": 0,
    "rgb_payload_bytes": 0
  }
}
```

关键变化：

- app 应直接发送 quaternion `[qw,qx,qy,qz]` 或 4x4 transform，Euler 只保留为 UI/debug 字段。
- app 应明确 `pose.frame` 和 `world_origin_id`，否则 exporter 只能猜坐标系。
- app 应输出 gripper 的连续物理量；`open_percent` 可以保留，但不应是唯一字段。
- app 本地 JSONL 和 TCP JSONL 应使用同一 schema，避免“本地能看、receiver 不能复现”的问题。

### 12.3 Mac iPhone receiver 需要改造什么

当前 receiver 在：

```text
modules/04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/iphone/
```

receiver 的目标不是直接写 UMI-FT Zarr，而是把 app packet 标准化成 iPhone source view。建议增加一个 V2 parser/normalizer：

```text
raw app packet
  -> normalized_iphone_frame
  -> alignment timeline
```

receiver 需要做：

| 改造项           | 说明                                                                                                                         |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| schema 分流      | 根据`schema_version` 解析 V1/V2；V1 只作为兼容路径。                                                                       |
| raw 完整落盘     | 原始 packet、payload/chunk、事件都保存，不能只保存 exporter 当前要用的字段。                                                 |
| pose 标准化      | 输出`pose7_qwxyz`、`camera_in_user_world_4x4`、`pose_frame`、`world_origin_id`、`tracking`、`world_calibrated`。 |
| timestamp 标准化 | 把 phone monotonic/capture time 对齐到 Mac`aligned_monotonic_ns`，记录 offset、latency、uncertainty。                      |
| RGB 标准化       | 保存 RGB payload 到文件，输出`image_path`、尺寸、intrinsics、encoding、payload status。                                    |
| gripper 标准化   | 输出`open_fraction_0_1`、可选 `width_m`、confidence、calibration id。                                                    |
| 错误显式化       | 缺 pose7、未 world calibrated、RGB payload decode 失败、gripper invalid 都要进入 quality/error 字段。                        |
| 不拥有 D435      | iPhone receiver 不生成`rgb_global_0` 或 `depth_0`；D435 receiver 单独负责全局相机源数据。                                |

推荐 normalized iPhone frame 结构：

```json
{
  "row_type": "normalized_iphone_frame",
  "schema_version": 2,
  "aligned_monotonic_ns": 123456789,
  "session_id": "iphone_YYYYMMDD_HHMMSS",
  "sequence": 12345,
  "rgb": {
    "image_path": "raw/iphone_stream/.../frame_001234.jpg",
    "width": 1920,
    "height": 1440,
    "intrinsics": {"fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0}
  },
  "pose": {
    "valid": true,
    "frame": "iphone_user_world",
    "world_calibrated": true,
    "world_origin_id": "mark_000001",
    "pose7_qwxyz": [0, 0, 0, 1, 0, 0, 0],
    "camera_in_user_world_4x4": []
  },
  "gripper": {
    "valid": true,
    "open_fraction_0_1": 1.0,
    "width_m": null,
    "confidence": 1.0
  },
  "quality": {
    "time_alignment": {},
    "errors": []
  }
}
```

### 12.4 D435 全局相机接入要求

D435 receiver 继续独立于 iPhone app。UMI-FT exporter 对它的目标 source view 是：

```json
{
  "row_type": "normalized_d435_frame",
  "aligned_monotonic_ns": 123456789,
  "rgb": {
    "video_path": "raw/global_camera/episode_000000/rgb.mp4",
    "frame_index": 12,
    "intrinsics": {}
  },
  "depth": {
    "raw_path": "raw/global_camera/episode_000000/depth.raw",
    "shape": [480, 640],
    "dtype": "uint16",
    "byte_offset": 0,
    "depth_scale_m": 0.001
  },
  "quality": {
    "timebase": "mac_aligned_monotonic",
    "errors": []
  }
}
```

exporter 写法：

- `rgb_global_0`：按 `rgb_time_stamps_0` 的每个 query time 找最近 D435 RGB。
- `rgb_global_time_stamps_0`：保存所选 D435 RGB 的时间。
- `depth_0`：按同一映射找 D435 depth，转成米、clip、resize。
- `map_to_d_idx_0`：如果 depth 与主时间轴一一写入，就是 identity；如果后续保留原始 D435 低频/异步 depth，则这里保存主时间轴到 depth array 的索引。

### 12.5 现在可以先占位的内容

在 UMI-FT V0 里可以先占位，但 manifest 必须说清楚：

| 字段                         | 占位方式              | 后续替换来源                                                           |
| ---------------------------- | --------------------- | ---------------------------------------------------------------------- |
| `ts_pose_command_0`        | `copy_ts_pose_fb_0` | 未来 robot/app command stream；或 UMI-FT 后处理规则。                  |
| `ts_pose_virtual_target_0` | `copy_ts_pose_fb_0` | UMI-FT force-based virtual target postprocess。                        |
| `stiffness_0`              | 常数 0 或配置常数     | UMI-FT stiffness estimation postprocess。                              |
| `gripper_0`                | width_m fallback      | 若 app 提供真实`width_m` 则优先使用；否则由 open fraction 线性映射。 |

这些占位允许我们先打通 UMI-FT replay buffer 与训练读取链路。当前 `wrench_left_0/right_0` 已在导出端套用原 UMI-FT TCP/tool-frame 外参，不再是 `_coinft_0` 直通字段。
