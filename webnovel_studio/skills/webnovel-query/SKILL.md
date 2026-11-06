---
name: webnovel-query
description: Use when the author invokes /webnovel-query to retrieve story settings, entity history, relationships, rules, open loops, or combined chapter context without modifying the project.
version: 1.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[character, foreshadowing, power level... / 角色名、伏笔、境界]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, query, canon, foreshadowing]
    related_skills: [webnovel-write, webnovel-doctor]
---

# /webnovel-query：项目查询

## 概览

保持 `/webnovel-query 伏笔`、`/webnovel-query 某角色在第20章的状态` 等命令体验。全程只读，只通过 `webnovel_runtime` 的 `query` 检索，不修改项目文件。

## 数据优先级

1. `.story-system/MASTER_SETTING.json`：全书主设定、调性与禁忌。
2. `.story-system/volumes/*.json`：卷级目标和节奏。
3. `.story-system/chapters/*.json`：章级目标、节点与动态上下文。
4. latest accepted `.story-system/commits/chapter_XXX.commit.json`：已发布事实。
5. 长期记忆、伏笔与时间线编排结果。
6. `.webnovel/state.json` 与 `index.db`：兼容投影，仅作 fallback/read-model。

合同和 accepted commit 缺失时必须明确说明已降级，不把投影误称为事实真源。

## 最窄查询分类

- 角色历史状态：`query_type=entity_state`，必须提供 entity 与 chapter。
- 实体关系：`query_type=relationships`，必须提供 entity 与 chapter。
- 世界规则：`query_type=rules`，可提供 domain。
- 伏笔或未闭合问题：`query_type=open_loops`。
- 综合跨类型上下文：`query_type=context`，必须提供 chapter；只有明确需要多类型联合时才用。
- 静态设定：先由运行时按上述优先级定位最窄文件片段，不默认加载全项目。

## Runtime 调用契约

从用户问题提取查询类型、实体、章节、领域与关键词，调用一次或必要的最少次数 `webnovel_runtime`，action 固定为 `query`，project_root 使用绝对项目根。多个互不依赖的最窄查询可以组合，但不得用全量上下文代替明确的单类查询。

无匹配时返回空结果并建议调整实体名、章节或范围；数据源缺失时说明缺少什么以及降级影响。查询错误不得触发写入或“自动修复”。

## 成功标准

- 项目根有效，查询类型和必要参数明确。
- 使用与问题匹配的最窄查询，不无故加载综合上下文。
- 结果标明实际数据源、匹配数量、关键路径或章节依据。
- 写前合同、写后 accepted commit 与投影之间的差异得到说明。
- 整个流程未修改任何项目文件。

## 作者友好最终报告

最终回复不贴原始结构化数据、异常堆栈或长日志，以一句总状态开头并使用固定三段式：

```text
总状态：已完成 / 部分完成 / 需要你处理 / 未完成。

一、产生的文件与完成情况
- 本命令只读，不产生文件；列出查询类型、数据源、匹配数量和结论。

二、过程中遇到的问题与异常耗时
- 已自动处理：安全的只读 fallback。
- 建议确认：同名实体、章节范围或投影差异。
- 必须处理：项目根无效或关键数据源缺失且无法回答。

三、下一步建议
- 可复制的收窄查询，或继续写作/审查的命令。
```

详细信息应结构化、简洁并附来源；无差异时可省略一致性小节。