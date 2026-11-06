---
name: webnovel-learn
description: Use when the author invokes /webnovel-learn to append one reusable, deduplicated writing pattern to project memory through the plugin runtime.
version: 1.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[writing lesson to remember / 要记住的写作经验]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, learning, project-memory, writing-patterns]
    related_skills: [webnovel-write, webnovel-query]
---

# /webnovel-learn：记录项目写作经验

## 概览

保持 `/webnovel-learn "本书有效的写法"` 的命令体验。把作者明确给出的经验，或本次对话中作者明确认可的写法，追加为项目级可复用模式。不得删除旧记录，不得直接编辑项目记忆文件。

## 提取规则

1. 有命令参数时以参数为准；参数为空时，只从本次对话中作者明确认可的写法提炼，不把助手自评当成认可。
2. 分类为 hook、pacing、dialogue、payoff、emotion、format 或 other；无法可靠分类时用 other。
3. 重要度为 high、medium 或 low，并保留完整、可执行的描述。
4. 当前章节可从只读项目状态取得；缺失时使用 null，不阻断学习。
5. 追加前按 `pattern_type + description` 做完全相同去重；完全重复时跳过并告知，部分相似不自动合并。

## Runtime 调用契约

只调用 `webnovel_runtime` 的 `query`，使用 project-memory 专用分支：payload 提供 `query_type=project_memory`、`operation=add_pattern`、pattern_type、description、category、importance 和可选 source_chapter。该分支名称保留统一 runtime action 面，不允许 Skill 直接写记忆文件。

运行时负责定位项目、初始化缺失的 patterns 容器、完全相同去重、原子追加与写后读取验证。若现有 JSON 损坏，必须停止且保持原文件不变；不得尝试拼接脏数据。

## 成功标准

- `.webnovel/project_memory.json` 存在且格式合法。
- 非重复模式已追加到 patterns 数组；重复模式明确返回 skipped，且未产生第二条记录。
- 返回结果包含 success 状态和完整 learned 对象，或清晰的重复/损坏原因。
- 旧记录未删除，Skill 未绕过运行时直接改文件。
- 缺当前章节时使用 null，学习本身仍可完成。

## 作者友好最终报告

最终回复不贴原始结构化数据、异常堆栈或长日志，以一句总状态开头并使用固定三段式：

```text
总状态：已完成 / 部分完成 / 需要你处理 / 未完成。

一、产生的文件与完成情况
- 记录的模式类型、完整描述、重要度、来源章节和追加/去重状态。

二、过程中遇到的问题与异常耗时
- 已自动处理：首次初始化 patterns 容器或完全重复跳过。
- 建议确认：分类或重要度不够明确。
- 必须处理：项目记忆 JSON 损坏或项目根无效。

三、下一步建议
- 在下一章继续应用该模式：/webnovel-write {next_chapter}
```

重复跳过是成功的幂等结果，但必须明确没有新增记录。