# docs 目录说明

这个目录放的是“全局规则”和“跨模块规范”。

如果你要理解整套系统怎么拼起来，看这里；如果你只是想做某一个模块，先看 `modules/`。

## 这里面有什么

- `system_architecture.md`
  - 整体架构
- `iphone_mac_realtime_stream_rewrite.md`
  - iPhone 1920x1440@30 主帧的 H.264、可靠传输、Mac 接收端重写规范
- `collection_workflow.md`
  - 采集前后流程
- `training_zarr_alignment.md`
  - 训练格式需求
- `training_format_implementation.md`
  - 导出实现约束
- `run_metadata_schema.md`
  - `RUN_METADATA.json` 规范
- `manual_review_and_trim.md`
  - 人工复核与裁剪 override
- `open_questions.md`
  - 还没完全定死的地方

## 什么时候看这个目录

- 你要改全局规则
- 你要定义模块之间的接口
- 你要确认训练格式
- 你要理解 run 目录和 metadata

## 不适合从这里开始的情况

如果你只是想单独推进：

- D435i 采集
- iPhone 采集
- CoinFT + Teensy
- 笔记本端对齐导出

那就先看 `modules/`。
