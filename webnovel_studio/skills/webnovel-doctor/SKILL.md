---
name: webnovel-doctor
description: Use when the author invokes /webnovel-doctor for stage-aware, read-only diagnosis of project files, data stores, dependencies, and dashboard assets (vector retrieval is disabled in this build).
version: 1.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[--chapter N] [--deep]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, diagnostics, health, read-only]
    related_skills: [webnovel-query, webnovel-dashboard]
---

# /webnovel-doctor：只读项目体检

## 概览

保持 `/webnovel-doctor`、`/webnovel-doctor --chapter 8` 与 `/webnovel-doctor --deep` 的命令体验。诊断目录、关键文件、JSON、SQLite、RAG 配置、依赖、合同树与 Dashboard 产物，但不写文件、不修复、不安装依赖、不启动面板。

## 原则

1. 先用短状态判断项目阶段，再做阶段感知检查。
2. init 刚结束的项目不按已写多章项目要求检查；缺失项必须结合当前 phase 与 target_chapter 解释影响。
3. 指定 chapter 时收窄到该章合同、正文、提交与投影；deep 模式才扩展依赖、数据库一致性和 Dashboard 产物检查。
4. 不展示或要求粘贴任何密钥；只说明配置是否缺失或不可用。
5. 诊断全程只读，发现问题后只给建议，不自动修复。

## Runtime 调用契约

严格按顺序调用同一个 `webnovel_runtime`：

1. `project_status`：传入绝对项目根和可选 chapter，取得 phase、target_chapter、当前运行与短 blocker。
2. `doctor`：传入同一项目根、可选 chapter，并在 payload 中传 deep 布尔值。

第二步必须基于第一步返回的阶段解释结果。项目根无效时停止，不猜测其他项目；短状态失败时不得继续做可能误导的深度体检。

## 检查与分类

- 项目结构和阶段应有目录。
- 状态、创意档案、主设定、卷/章合同、正文、提交和投影的存在性与格式。
- SQLite 可读性和必要表。
- 向量检索（RAG）在本插件中已关闭：如上游报告 RAG 未配置，这是预期状态，不是故障，不要建议用户配置任何 API Key。
- Python 运行依赖与 Dashboard 前端产物。
- 当前 run 是否失败、可恢复或被锁占用。

每个问题都要给路径、严重度、对当前阶段的影响与最小修复建议，并区分 blocker 与非阻断建议。

## 成功标准

- 返回当前 phase、target_chapter 与检查范围。
- 明确是否有 blocker，并列出缺失或异常路径。
- RAG、依赖、SQLite 与 Dashboard 配置状态按请求深度得到说明。
- 每个问题都有阶段化影响和最小修复建议。
- 整个过程没有写项目文件、安装依赖或启动服务。

## 作者友好最终报告

最终回复不贴原始结构化数据、异常堆栈或长日志，以一句总状态开头并使用固定三段式：

```text
总状态：已完成 / 部分完成 / 需要你处理 / 未完成。

一、产生的文件与完成情况
- 本命令只读，不产生文件；报告 phase、target_chapter、检查范围和健康项。

二、过程中遇到的问题与异常耗时
- 已自动处理：无；体检不做修复。
- 建议确认：非阻断配置、依赖或数据完整性警告。
- 必须处理：blocker、损坏数据、关键文件缺失或运行锁异常。

三、下一步建议
- 按严重度给出最小修复动作和可复制的后续 Skill 命令。
```

不得把原始异常直接丢给作者，也不得把“检查完成”误写成“问题已修复”。