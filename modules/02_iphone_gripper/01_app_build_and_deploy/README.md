# Part 01：App 构建与部署

这一部分只回答：

- iPhone 采集 App 怎么构建
- 怎么装到真机
- 怎么自动启动
- 怎么把录制结果拉回电脑

## 入口

- App 源码：
  - [ios_app/UMIFTiPhoneCaptureCore](</Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/ios_app/UMIFTiPhoneCaptureCore>)
- 当前 Mac 端接收：
  - [../04_laptop_alignment_export/entrypoints/iphone_stream_receiver.py](/Users/550m/code/UMIFT-datacollect/modules/04_laptop_alignment_export/entrypoints/iphone_stream_receiver.py)
  - [../04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/iphone/archive_receiver_v2.py](/Users/550m/code/UMIFT-datacollect/modules/04_laptop_alignment_export/umift_laptop_alignment/capture/receivers/iphone/archive_receiver_v2.py)
- 旧 MVP 脚本：
  - [tools/automate_capture_session.py](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/tools/automate_capture_session.py)
  - [tools/pull_latest_iphone_demo.py](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/tools/pull_latest_iphone_demo.py)
  - 这些脚本只服务 `ios_app/legacy/UMIFTiPhoneCaptureMVP`，需要显式 legacy 参数才能运行。

## 推荐先看

- [ios_app_status.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/01_app_build_and_deploy/ios_app_status.md)
- [automation_debugging.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/01_app_build_and_deploy/automation_debugging.md)
- [ios_app/UMIFTiPhoneCaptureCore/README.md](/Users/550m/code/UMIFT-datacollect/modules/02_iphone_gripper/ios_app/UMIFTiPhoneCaptureCore/README.md)

## 当前边界

已确认可自动化：

- build
- install
- launch
- pull app container 中的 demo

未确认可稳定自动化：

- 锁屏解锁
- 直接从系统照片库批量拖照片

如果只是要把相册里的参考图拉到电脑，当前更稳的是：

1. 手动同步到 `Pictures`
2. 再让本工作区消费这些图片
