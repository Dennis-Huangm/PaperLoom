# 双源协作修复与验证

日期：2026-09-16。

## 已启用的行为

当前全局配置和活动 `agentict2i` 研究方向均已设为 `provider: hybrid`。
原有关键词、回溯 45 天、arXiv 500 篇候选、预筛 50 篇、活动方向推荐 20 篇等设置保持原值。
alphaXiv 独立候选上限为 15，语义候选最低精确概念组命中数为 1，且不超过通用门槛。

每次运行依次执行两源检索、合并 ID、通过 arXiv 批量补全语义候选，再进行历史过滤和排序。
两源主动参与不依赖前一路失败。当前采用顺序调用，沿用 arXiv 共享节流器。

- `hybrid`：两源每次共同参与；alphaXiv 每次调用会使用其助手额度。
- `auto`：保留旧配置的异常降级行为。
- `arxiv`：只调用 arXiv。
- `alphaxiv_fallback_enabled`：为兼容保留旧字段名，控制 auto 和 hybrid 两种模式的 alphaXiv 开关。

## 审核问题处理

| 审核项 | 修复 |
| --- | --- |
| F1 先截断后去重 | 历史与负面反馈过滤在预筛截断前完成；无新候选不覆盖同日已有日报 |
| F2 共享文件影响筛选 | 不再读取/删除 `discovery-source.txt`；使用逐篇来源和运行内结果，诊断文件按方向与运行隔离 |
| F3 日期、版本和摘要失真 | 未知日期/版本保留未知，预览标记 partial；补全后重新验证窗口；原始版本后缀保留 |
| F4 旧快照无法恢复 | 日报与单篇报告共用补全/缓存逻辑；报告遇到 partial 快照时尝试 arXiv，恢复后缓存完整数据；核验来源随实际来源记录 |
| F5 force 不生效 | force 路径不传历史排除，预筛阶段也不排除历史；负面反馈仍生效 |
| F6 Retry-After 被截断 | 支持秒数及 HTTP-date，不缩短有效服务端冷却时间 |
| F7 MCP 生命周期不完整 | 发送 initialized 通知，接受 202 空正文，检查协商版本；SSE 拼接多行事件并按请求 ID 选择响应 |

旧 partial 快照按需补全，不批量重写历史日报。完整元数据缓存在 `run/metadata-cache/`；
日常发现正常联网时，超过一天的缓存会重新补全；arXiv 故障时允许复用旧的完整缓存。
单篇报告仍可复用完整快照，版本持续监控继续由原有版本追踪功能负责。

新版 `Paper` 明确保存发现来源、正式数据来源、完整性和摘要类型，允许未知日期/版本，
沿用现有持久化模型以减少迁移范围。历史记录缺少完整性字段时根据已有字段保守判断。

## 输出与配置界面

- 新增协作模式、alphaXiv 独立预算、语义候选概念组门槛。
- 仪表盘展示逐篇发现来源和待补全状态；报告/日报对预览摘要和未知日期做明确标记。
- `discovery-<profile>-<run>.json` 包含来源状态、耗时、召回/合并/补全/日期拒绝/预筛/选中数量。
- `candidates-<profile>.json` 和 `index-<profile>.html` 按方向保存；保留旧 `index.html` 兼容入口。
- Web 日报任务在提交时固定研究方向配置，避免排队期间切换方向后执行错方向。

## 验证

最终完整测试：**133 passed, 1 warning**（10.41 秒）；Python compileall 和 app.js 的 Node 语法检查通过。
唯一警告来自已安装 Starlette TestClient 对 httpx 的弃用提示。

新增回归覆盖：双源合并、arXiv 空结果、单源/双源失败、force 传递、先去重后截断、
方向隔离、旧快照修复、过期缓存更新、未知/越界日期、MCP 通知与 SSE、协议版本拒绝、
长 Retry-After、批量查询超过 50 个 ID、设置保存/活动方向回读、部分数据的报告渲染。

真实服务验证（仅写入 `work/hybrid-live-check`）：

- MCP initialize / initialized / tools/list 均成功；协商版本为 `2025-03-26`。
- 缩小预算为 arXiv 15 / alphaXiv 5，alphaXiv difficulty=1，执行一次发现。
- arXiv 返回 15 篇；alphaXiv 返回 5 篇，均为该批 arXiv 结果之外的候选。
- 5 篇全部由 arXiv 补全，合并后 20 篇均为完整数据，无日期拒绝。
- 本次未执行真实 LLM 精排、邮件、Zotero 或 Obsidian 写入，也未发布正式日报。

测试命令：

```powershell
python -m pytest -q -p no:cacheprovider --basetemp work/pytest-hybrid-verified
python -m compileall -q src/arxiv_ra tests
```

alphaXiv 真实检索验证只覆盖 difficulty=1 的小预算请求，不构成所有查询的可用性保证。
现有 arXiv 节流为进程内共享；同时启动多个独立 CLI/GUI 进程时仍需控制总请求速率。

## 备份与生效

改动前 Python 源码、模板、测试、配置和研究方向备份：`work/before-hybrid-fix.zip`，未包含 `.env` 或 static 目录。
旧版 release 中的 wheel/zip 保持原样；本次修改针对当前源码工作目录。
已运行的 GUI 进程需要重启以加载新 Python 代码；从项目源码启动时会使用新的协作配置。
