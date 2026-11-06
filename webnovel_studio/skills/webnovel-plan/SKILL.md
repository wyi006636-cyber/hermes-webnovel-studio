---
name: webnovel-plan
description: Use when the author invokes /webnovel-plan to create a practical volume outline made of story events, character choices, and chapter-ending momentum.
version: 2.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[卷号] [可选章节范围] [特殊要求]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, novel-writing, outline]
    related_skills: [webnovel-init, webnovel-write]
---

# /webnovel-plan：规划一卷小说

## 目标

生成一份能直接拿来写正文的卷纲。每章只回答三个创作问题：发生什么，人物做了什么选择，这个选择怎样把读者推向下一章。

## 执行

每条作者指令只调用一次 `webnovel_runtime`：

- action：`project_plan`
- project_root：当前小说项目绝对路径
- volume：卷号
- payload.start_chapter / end_chapter：作者明确给范围时才传；否则每卷默认 30 章
- payload.user_requirements：作者的自然语言要求，可以是字符串、列表或对象
- payload.confirmed_overwrite：只有作者明确授权覆盖已有卷纲时才为 true

不要求作者先补齐目标字数、时间体系、角色层级、势力表、境界表、Strand、爽点密度或结构节点。

## 规划内容

卷纲包含：

- 本卷人物和剧情发生的总体变化。
- 每章章名。
- 每章真正发生的事件。
- 人物在本章必须做出的选择。
- 由本章事件自然产生的结尾推动。

规划关注剧情因果、人物关系、冲突升级、节奏变化和情绪积累。时间只要故事内部说得通，不生成时间字段验收表。

不生成 CBN、CPN、CEN、source_id、逐字短语、must_cover、forbidden、审查合同、总纲写回 JSON、Story System 或向量数据。

## 漏章处理

模型第一次遗漏章节时，运行时只补缺失章节，不重写已经生成的整卷，也不允许补章结果覆盖已有章节卡。运行时最多再做两次内部补漏；仍缺少真实事件、人物选择或结尾推动时停止且不发布半成品，不用空洞模板冒充规划成功，也不让作者重新提交整卷。

## 已有卷纲

已有卷纲时不擅自覆盖。作者明确要求重做该卷后，再传 `confirmed_overwrite=true`。

## 成功标准

- `大纲/第N卷-详细大纲.md` 包含范围内全部章节。
- 每章都有实际事件、人物选择和结尾推动。
- `大纲/第N卷-节拍表.md` 只保留本卷总体变化和章节范围。
- 可以直接执行 `/webnovel-write {首章}`。

## 作者友好最终报告

只报告卷名、章节范围、卷纲路径和下一步写章命令，不输出 schema、节点统计、合同刷新或机器验收状态。

```text
第N卷已规划：卷名
章节范围：第A章至第B章
详细卷纲：绝对路径
下一步：/webnovel-write A
```