---
name: webnovel-dashboard
description: Use when the author invokes /webnovel-dashboard to start the local read-only project dashboard and verify its runtime health endpoints.
version: 1.0.0
author: Webnovel Writer Hermes contributors
license: GPL-3.0-only
platforms: [linux, macos, windows]
allowed-tools: [webnovel_runtime]
argument-hint: "[--port N]"
metadata:
  hermes:
    requires_tools: [webnovel_runtime]
    tags: [webnovel, dashboard, visualization, read-only]
    related_skills: [webnovel-query, webnovel-doctor]
---

# /webnovel-dashboard：只读管理面板

## 概览

保持 `/webnovel-dashboard` 的命令体验，可自定义端口。运行时固定以 no-browser 模式启动，只返回已验证的本地 URL，不替作者打开浏览器。面板展示创作进度、设定词典、关系图谱、章节、追读力和 Story Runtime 主链状态。启动与路径解析只通过 `webnovel_runtime`，Skill 不自行启动进程。

## 安全边界

- 面板纯只读，不提供项目修改接口，不修改项目文件。
- 文件访问限制在解析后的绝对项目根。
- 默认只绑定 localhost，不暴露到局域网或公网。
- 不默认安装依赖；缺依赖时只报告精确缺项和用户可选的安装建议。
- 不自动处理权限、密码、密钥或网络暴露。

## Runtime 调用契约

调用 `webnovel_runtime` 的 `dashboard_start`，传入绝对项目根；payload 可包含 operation 与 port，不接受 no_browser。未指定端口时让运行时选择可用端口。运行时始终以 no-browser 模式执行，负责验证前端产物、依赖、项目根、只读配置和进程启动，并返回进程标识与实际 URL。

启动前由运行时执行 upstream preflight，启动后确认 `/api/story-runtime/health` 可用。端口占用时自动选择其他 loopback 端口；前端产物、依赖或项目数据缺失时不得伪造成功 URL。

## 成功标准

- Dashboard 进程已启动，返回可访问的 localhost URL 和进程标识。
- health 与 preflight 检查通过。
- 页面能够读取章节列表、实体关系和项目状态；数据缺失时明确指出缺失来源。
- 面板保持只读，文件访问未越出项目根。
- 未修改任何项目文件，未自动安装依赖。

## 作者友好最终报告

最终回复不贴原始结构化数据、异常堆栈或长日志，以一句总状态开头并使用固定三段式：

```text
总状态：已完成 / 部分完成 / 需要你处理 / 未完成。

一、产生的文件与完成情况
- 不产生项目文件；报告实际 URL、进程状态、health 与 preflight 结果。

二、过程中遇到的问题与异常耗时
- 已自动处理：安全更换空闲端口。
- 建议确认：是否打开浏览器或改用指定端口。
- 必须处理：缺依赖、缺前端产物、项目根无效或健康检查失败。

三、下一步建议
- 打开返回的本地 URL，或运行 /webnovel-doctor 继续排查。
```

不得只说“已启动”而不返回实际 URL 和健康状态。