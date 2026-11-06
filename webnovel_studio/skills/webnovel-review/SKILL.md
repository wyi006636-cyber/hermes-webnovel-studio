---
name: webnovel-review
description: Use when the author invokes /webnovel-review to receive an optional craft review focused only on story, character, dialogue, pacing, emotion, and literary expression.
version: 2.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[from chapter] [optional to chapter] / [起始章] [结束章]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, novel-writing, literary-review]
    related_skills: [webnovel-write, webnovel-query]
---

# /webnovel-review：小说质量审读

## 目标

为作者提供可选的小说编辑意见。报告只服务于作者判断，不决定正文是否“通过”，不改变任何章节的完成状态，也不自动修改正文。

## 执行

调用一次 `webnovel_runtime`：

- action：`review_range`
- project_root：当前小说项目绝对路径
- payload.start_chapter：起始章
- payload.end_chapter：结束章；未给时与起始章相同

每章单独生成一份 `审查报告/第NNNN章-小说质量报告.md`。

## 只审小说质量

报告只讨论：

- 剧情推进、因果和人物选择。
- 人物动机、关系变化和人物声音。
- 对白、潜台词和人物辨识度。
- 场景张力、节奏与信息释放。
- 情绪递进、动作、感官和细节。
- 叙述视角、句式、意象和文学表达。
- 章末吸引力和前后连续性。

报告先写真正有效的部分，再写少量具体可执行建议。不要给“通过/不通过”、总分、阈值或 blocking verdict。

字数、Markdown、字段、schema、SHA、source_id、CBN/CPN/CEN、锚点、索引、向量、投影和备份都不是小说质量，不得出现在审读标准里。

## 非阻断

- 审读失败只返回 warning，不影响正文。
- 找不到某章只报告缺失，不把其他章节判失败。
- 不自动重写正文，不更新正文 SHA，不触发提取或提交。
- 作者是否采纳建议，由作者自己决定。

## 成功标准

- 找到的每章都有一份可读的质量报告。
- 正文文件逐字节不变。
- 返回 `reviewed` 或 `reviewed_with_warnings`，不返回“未通过”。

## 作者友好最终报告

只报告审读范围、报告路径和 warnings，不输出内部模型数据或验收表。

```text
小说质量审读已完成：第A章至第B章
报告：绝对路径
正文未修改。
```