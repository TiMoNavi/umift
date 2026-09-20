# 项目目录地图

如果你打开项目后感觉“这些文件夹都在讲项目，但我不知道先看哪个”，先看这张地图。

## 一句话分工

```text
modules/   = 按模块开工的入口
docs/      = 全局规则和流程
reference/ = 外部参考资料
tools/     = 实际脚本
schemas/   = 输出格式硬约束
runs/      = 真实采集结果
```

## 推荐阅读顺序

### 情况 A：我要直接干某个模块

先看：

```text
modules/
```

再按需要跳到：

- `docs/`
- `reference/`
- `tools/`

### 情况 B：我要理解整个项目

先看：

1. `README.md`
2. `modules/README.md`
3. `docs/system_architecture.md`
4. `docs/collection_workflow.md`

### 情况 C：我要写代码

先看：

1. 对应 `modules/<module>/README.md`
2. 对应 `docs/*.md`
3. `tools/`

## 当前顶层目录

### `modules/`

主入口。推荐默认从这里开始。

### `docs/`

放全局规范，不是分模块入口。

### `reference/`

放背景资料，不是当前项目的直接实现要求。

### `tools/`

放脚本实现。

### `schemas/`

放导出格式硬约束。

### `runs/`

放真实数据，不是文档。

## 当前最重要的事实

现在这个项目已经不再使用旧的 `hardware/` 目录作为主入口。

如果你看到有文档还提到 `hardware/`，那就是旧引用，应该以：

```text
modules/ + docs/ + reference/ + tools/
```

这一套为准。
