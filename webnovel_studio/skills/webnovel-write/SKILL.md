---
name: webnovel-write
description: Use when the author invokes /webnovel-write to write one complete novel chapter from the project's outline, settings, prior prose, and current instruction.
version: 2.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[chapter] [extra instructions] [--rewrite] / [章号] [本章补充要求] [--rewrite]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, novel-writing, chapter]
    related_skills: [webnovel-plan, webnovel-review, webnovel-query]
---

# /webnovel-write：写一章小说

## 目标

作者给出章号后，直接写出一章可读小说正文并保存到 `正文/`。正文是第一交付物，不让作者处理内部字段、合同、审查状态或数据投影。

## 执行

每条作者指令只调用一次 `webnovel_runtime`，action 固定为 `write_chapter`：

- `project_root`：当前小说项目绝对路径。
- `chapter`：作者给出的章号。
- `payload.instructions`：章号之外的自然语言要求；没有则为空字符串。
- `payload.overwrite`：只有作者明确输入 `--rewrite`、明确要求重写或明确授权覆盖时才为 true。
- `payload.target_chars`：只有作者明确提出目标字数时才传；它只是软方向。

不得调用 `write_prepare`、`write_draft`、`write_review`、`write_revise` 或 `write_commit`。不得要求或转述 `run_id`。

## 小说质量

系统内部只做一次起草和一次文学编辑。编辑只关心：

- 剧情是否推进，因果和人物选择是否成立。
- 对白是否符合人物、关系和情绪，是否有潜台词。
- 场景张力、节奏和信息释放是否自然。
- 情绪是否通过动作、感官和细节积累。
- 人物声音、关系变化和前后连续性。
- 叙述视角、语言质感、意象、句式和文学表达。
- 章末是否产生具体的下一章阅读欲望。

字数、Markdown、字段类型、schema、SHA、source_id、逐字锚点、CBN/CPN/CEN、索引、向量和投影都不是小说质量，不得因为这些内容打回、重写或拒绝保存正文。

## 自动降级

- 没有轻量记忆时直接读取上一章正文。
- `.webnovel/state.json` 缺失或损坏时把它当作 warning，继续使用设定集、大纲、前文和作者指令写作。
- 没有 Story System、index.db、RAG 或向量时照常写。
- 文学编辑失败时保存初稿。
- 记忆提取失败时正文仍算写成，只报告 warning。
- 正文偏长或偏短时照常保存，不裁剪、不补写、不打回。

只有项目路径不安全、目标正文保护要求停止（已存在但未授权覆盖，或生成期间发生并发变化）、Writer 一次正常调用和一次技术重试都没有返回可用正文时停止。

## 已有正文

如果 runtime 返回 `chapter_exists`，不要擅自覆盖。告诉作者该章已存在；作者明确要求重写后，再以 `overwrite=true` 调用一次新的 `write_chapter`。覆盖前运行时会备份旧正文。

## 成功标准

- `正文/第NNNN章-章名.md` 存在且包含非空小说正文。
- 返回 `written` 或 `written_with_warnings`。
- 编辑或记忆维护失败不影响正文成功状态。
- 最终回复只报告章名、正文路径、正文字符数、可理解的 warnings 和下一章命令。

## 作者友好最终报告

用简短自然语言汇报：

```text
第N章已写完：章名
正文：绝对路径
正文字符数：N
需要注意：只有存在 warning 时列出
下一章：/webnovel-write N+1
```

不输出 JSON、schema、SHA、模型审查表、投影状态、内部调用链或长日志。