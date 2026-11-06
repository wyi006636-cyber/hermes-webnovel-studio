---
name: webnovel-init
description: Use when the author invokes /webnovel-init to turn one idea into a practical novel proposal and create a human-readable writing project after one confirmation.
version: 2.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[书名、灵感或一句话故事]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, novel-writing, initialization]
    related_skills: [webnovel-plan, webnovel-write]
---

# /webnovel-init：创建小说

## 目标

作者给一句灵感就能得到一份可写的小说方案。系统最多请求一次总确认，不逐项追问书名、字数、平台、金手指、境界、反派层级或创意约束。

## 第一次调用：生成方案

调用一次 `webnovel_runtime`：

- action：`project_init`
- project_root：准备创建的新项目绝对路径
- payload.confirmed：false
- payload.idea：作者原始输入；没有输入时传“请创造一部人物驱动、有长期冲突的中文长篇小说”

运行时只返回一份完整提案，不创建项目目录。提案只包含：书名、题材、一句话故事、主角、核心冲突、世界压力、叙事调性、开篇事件和长期故事动力。

把整份提案用自然语言展示给作者，只问一次是否采用。作者可以直接确认，也可以在一次回复里修改任意部分。不要把提案拆成多轮字段问答。

## 第二次调用：确认后创建

作者确认后，再调用一次 `project_init`：

- payload.confirmed：true
- payload.proposal：确认后的完整提案对象

只创建写小说真正需要的内容：

- `.webnovel/state.json`
- `.webnovel/creative_proposal.json`
- `设定集/核心设定.md`
- `大纲/总纲.md`
- `正文/`
- `审查报告/`

不生成 Story System、MASTER_SETTING、合同树、索引、向量、时间线、审查节点或创作约束包。

## 停止条件

只有项目路径不安全或目标项目已经初始化时停止。创意方案缺少某个字段时由创作模型给出合理方案，不把字段缺口抛给作者。

## 成功标准

- 核心设定和总纲是人能直接阅读、继续修改的 Markdown。
- state 只记录项目信息和写作进度。
- 项目创建后可以直接执行 `/webnovel-plan 1`。

## 作者友好最终报告

确认创建后只报告书名、项目目录、核心设定、总纲和下一步命令，不输出内部数据结构或验收表。

```text
小说项目已创建：书名
项目：绝对路径
核心设定：绝对路径
总纲：绝对路径
下一步：/webnovel-plan 1
```