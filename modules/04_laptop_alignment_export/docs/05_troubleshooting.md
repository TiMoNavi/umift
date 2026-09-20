# 故障排查

## 1. 先确认使用正确入口

从 Module 04 根目录启动：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=. \
python3 entrypoints/receiver_web_gui.py --host 127.0.0.1 --port 8767
```

不要从 `legacy/` 启动 GUI，也不要手工编辑 Dataset 中的 `RUN_MANIFEST.json`。

## 2. Teensy / CoinFT 不在线

```bash
ls -1 /dev/cu.usbmodem*
python3 entrypoints/receiver_web_gui.py --help | rg coinft
```

确认串口没有被 Arduino Serial Monitor 或另一个 GUI 进程占用。模型加载失败时，检查 GUI 选择的模型集合目录是否包含 `model_set.json`、左右 ONNX、norm JSON 和 ONNX 实际引用的 external data 文件。

## 3. D435 不在线

```bash
bash umift_laptop_alignment/capture/receivers/d435/run_d435_macos.sh list
```

设备可枚举但 GUI 启动失败时，重新检查固定 sudo 控制入口；不要给整个 Python 进程通用免密 sudo。

## 4. iPhone 不在线

保持手机解锁并信任这台 Mac，确认 App 已授予相机和本地网络权限。检查实体设备：

```bash
xcrun devicectl list devices
python3 entrypoints/iphone_stream_receiver.py --help
```

重连时不要删除 iPhone spool；receiver 的累计 ACK 用于恢复未确认片段。

## 5. 录制 Gate 失败

依次检查 GUI 的三路 freshness、buffer item 数、最近时间戳和错误字段。`record_start` 只是声明有效窗口，三路设备应在按下录制前持续采集。任何一路时间戳不新鲜都不能强行进入正式 Episode。

## 6. Normalize / Align 失败

只对单个 Episode 执行：

```bash
python3 entrypoints/normalize_run.py --help
python3 entrypoints/align_run.py --help
```

检查 `RUN_MANIFEST.json` 中 Episode 是否完整关闭、三路 raw 目录是否存在，以及 aligned monotonic timestamp 是否单调递增。不要通过复制其他 Episode 的中间结果绕过失败。

## 7. UMI-FT Zarr 导出失败

独立 exporter 入口：

```bash
python3 entrypoints/export_run.py <run_dir> <episode_index> \
  --format umift-replay-buffer-zarr \
  --config config/export_profiles/umift_replay_buffer_v0.json
```

导出前必须完成 normalize 和 align。成功后检查 `exports/umift_replay_buffer_zarr/` 下的 `EXPORT_CONFIG.json`、`UMIFT_EXPORT_MANIFEST.json` 和 Zarr，并使用 `entrypoints/zarr_inspector_web.py` 或测试中的 catalog 检查结构。

## 8. 交付前回归

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=. \
python3 -m pytest -q -p no:cacheprovider
```

除单元测试外，还必须完成一次真实 iPhone、Teensy/CoinFT、D435 Episode，核对三路时间戳和独立 UMI-FT Zarr 导出。
