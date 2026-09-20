# RUN_METADATA 规范

这份文档定义每个 `run` 目录下的元数据文件：

```text
runs/<run_id>/RUN_METADATA.json
```

目标很简单：

1. 让每次采集都可追溯
2. 让对齐和导出脚本不用猜设备信息
3. 让人工 override 有地方落

## 必须存在

每个 run 至少要有：

```text
runs/<run_id>/
├── RUN_METADATA.json
└── MANUAL_TRIM.json
```

其中：

- `RUN_METADATA.json` 记录采集事实
- `MANUAL_TRIM.json` 记录人工修正和裁剪 override

## 最小 schema

```json
{
  "run_id": "20260614_150000_pick_test",
  "created_at": "2026-06-14T15:00:00+08:00",
  "task_name": "pick_test",
  "operator": "",
  "notes": "",
  "hardware": {
    "global_camera": {
      "model": "Intel RealSense D435i",
      "serial": "",
      "rgb_enabled": true,
      "depth_enabled": true,
      "imu_enabled": false,
      "rgb_width": 640,
      "rgb_height": 480,
      "rgb_fps": 30,
      "depth_width": 640,
      "depth_height": 480,
      "depth_fps": 30
    },
    "iphone": {
      "model": "iPhone 15 Pro",
      "ios_version": "",
      "app_build": "",
      "rgb_enabled": true,
      "depth_enabled": true,
      "pose_enabled": true,
      "reset_origin_used": true,
      "origin_offset_xyz_rpy": [0, 0, 0, 0, 0, 0]
    },
    "teensy": {
      "model": "Teensy 4.1",
      "firmware_name": "",
      "firmware_version": "",
      "usb_port": ""
    },
    "coinft_left": {
      "connected": true,
      "calibration_ref": "",
      "onnx_ref": "",
      "norm_ref": ""
    },
    "coinft_right": {
      "connected": true,
      "calibration_ref": "",
      "onnx_ref": "",
      "norm_ref": ""
    }
  },
  "capture_policy": {
    "primary_timeline": "iphone.poseTimes",
    "record_depth_arm": true,
    "record_depth_fix": true,
    "export_depth_default": false,
    "force_frame": "coinft",
    "force_export_side": null,
    "start_rule": "first_close_plus_3s",
    "end_rule": "stable_pose_3s"
  },
  "outputs": {
    "iphone_demo_dir": "",
    "global_camera_dir": "",
    "coinft_dir": "",
    "gripper_width_dir": "",
    "aligned_dir": "",
    "export_dir": ""
  }
}
```

## 当前必须写死的约束

### 全局相机

当前你手里是 `D435i`，因此建议：

```json
"model": "Intel RealSense D435i",
"imu_enabled": false
```

也就是说：

- 允许以后用 IMU
- 但第一版明确不采 IMU

### iPhone

必须写：

```json
"depth_enabled": true
```

因为现在项目规则是：

- iPhone depth 采集必开
- 训练导出默认不写 depth

### CoinFT

必须区分左右：

```json
"coinft_left": {...},
"coinft_right": {...}
```

不能只写一个总的 `coinft`，否则后面很难追溯哪一路被导出。

### force_export_side

第一版不做 fusion，因此：

```json
"force_export_side": "left"
```

或：

```json
"force_export_side": "right"
```

采集开始时可以先留空，导出前补上。

## 建议填写时机

### 采集开始前

先写：

- `run_id`
- `task_name`
- 设备型号
- 分辨率和 FPS
- `depth_enabled`
- `imu_enabled`
- `force_frame`

### 对齐后

补写：

- `force_export_side`
- 实际输出目录
- 采集备注

### 导出后

补写：

- 导出的 task 名
- validator 结果
- 任何人工 override 说明

## 设计取舍

这个 schema 故意不做得太重，不追求一次把所有未来字段都想完。第一版只服务两个目标：

1. 别让脚本猜设备事实
2. 别让你过几天回来看不懂这次 run 到底怎么采的
