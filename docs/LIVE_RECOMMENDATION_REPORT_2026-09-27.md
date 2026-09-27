# 新推荐到全文报告的真实链路验收

日期：2026-09-27。使用统一后的项目 `.venv`、正式 PDF/模型设置及当前代理环境。此轮承接 arXiv 406 修复，验证新推荐可以从正式 Web 报告接口进入全文流程。

## 范围与输入

- 选取上一轮真实日报中的 **RULER: Instance-aware Rubric Rewards for SVG Generation，2609.25270v1**，研究方向 `agentict2i`。
- 推荐 JSON 原样复制到隔离项目，通过 FastAPI TestClient 调用实际 `POST /api/jobs/report`，传入 `origin=recommendation`、原推荐日期和方向；没有手工构造替代论文快照。
- 实际调用元数据、PDF、方法图、模型服务及正式后台队列。QA 包装器仅记录调用状态并在工作线程内限制最多 24 次模型请求、单请求 180 秒、SDK 隐式重试关闭，不替换响应。
- 请求模型 `gemini-3.8-flash-high`，服务返回模型标识 `gemini-3.8-flash`。
- 隔离目录 `work/live-recommendation-report-20260927/`。邮件、Zotero、Obsidian 关闭；项目调度关闭，没有注册 Windows 计划任务。
- 正式 `.env`、`config.yaml`、当前方向和方向配置前后哈希一致；密钥未写入 QA 记录。

## 执行结果

任务 `d7ceac59f213` 于本地时间 08:59:39–09:04:45 完成，约 5 分钟。

| 检查 | 结果 |
| --- | --- |
| 报告入口、固定版本 | HTTP 202；持久请求保存原推荐 v1 |
| 重复提交 | 合并到同一活动任务 |
| PDF / 解析 | 19 页全部解析，Docling，92,643 字符 |
| 图片 | 3 张选定方法相关图，3 次真实视觉解读，文件齐全 |
| 正文 | 7 个分片全部完成，最终整合完成；质量标记 `full` |
| 模型请求 | 11 次：3 次视觉、7 次分片、1 次整合；全部成功，无超时、重试或预算阻断 |
| 引用 | 58 条定位成功，0 条定位拒绝，涉及 15 页；公式修复后 133 个 PDF 页码链接范围合法 |
| 页面结构 | 2 张表、97 个列表项、66 处公式；目录目标齐全，表格列数一致 |
| 资源 | HTTP 报告、图片、PDF、脚本和样式均 200；独立 HTML 的相对资源文件也存在 |
| 重启 | 任务 API 记录完全一致；首页、任务页、报告库及报告仍返回 200；私有台账返回 404 |

这里的完整报告和引用定位指标不代表所有结论准确。任务实际状态为 **`succeeded_with_warnings`**，原因是数值依据检查提示，详情见下节。

## 本轮发现并修复：公式内部的 Markdown 引用

模型在四个展示公式中写了 `\text{[[证据ID:…]]}`。引用定位后变成 `\text{[原文 …](paper.pdf#page=5)}`，Markdown 链接仍留在 LaTeX 公式内部。

使用项目内置 KaTeX 在 Node 中直接解析真实报告的 66 个公式，修复前 **4 个失败**。最小输入 `x=1 \text{[原文](paper.pdf#page=5)}` 也失败；移除引用后通过，因此不是 PDF 下载、样式加载或普通数学表达式的问题。

修复在 `markdown_rendering.py` 的共用公式 token 渲染边界实施：只识别项目格式的本地 PDF 引用，将其移到公式外并生成可点击链接，清理因移动而留下的空文本包装和尾部间距。普通数学文本、代码示例和任意外部链接不按此规则改写，原有净化继续生效。

该处理覆盖新报告、共用渲染器的其他报告类型，以及 GUI 从历史 Markdown 呈现的报告。本轮独立 HTML 用原始 Markdown 重新渲染；Markdown、证据和模型检查点未改写，未新增模型调用。

- 7 个回归用例先失败后通过，覆盖实际证据定位→公式渲染链路、行内/块级分隔符、文本包装、代码保留及危险链接不被转换。
- 真实报告回放后，**66 个公式全部通过 KaTeX 严格解析**，公式外的 PDF 链接保留。
- 相关渲染与引用测试 **47 passed**；全量回归 **517 passed，1 warning，81.90 秒**。唯一警告为已有 Starlette TestClient/httpx 弃用提示。compileall、report.js 语法检查和 diff 空白检查通过。

## 数值检查与剩余边界

数值检查报告 7 个问题条目：1 行表格的 3 个单元格、6 个定量文本块被暂不展示；原始内容保存在 `evidence.json`。不是下载失败或摘要回退。

人工辅助核对 PDF 第 6 页 Table 2：主表 9 个方法行仍展示的 **87 个数值逐项一致**。被隐藏的 VectorFusion 前 3 个数字也存在于原表，但所引用的固定片段没有覆盖它们。另一些训练环境、熵系数等数字存在于其他原文位置，模型没有为所在陈述提供覆盖完整的引用。因此不能把这 7 条提示解释为 7 个事实错误，也不能据此声称所有自动核验都已完成。

仍有部分表头、单位和条件对应检查显示为未评估，报告保留相应提醒。后续可改进完整行引用和复现参数的就近引用，减少有原文依据却因引用不足被隐藏的内容；本轮没有放宽校验或手工填回数值。

本轮完成结构、资源、公式解析、实际 API 和重启检查，**没有重新进行浏览器桌面/窄屏视觉验收**。没有覆盖任意论文、服务持续可用性或无人值守定时触发；尚未开启定时任务。

## 验收产物

- `work/run_recommendation_report_20260927.py`：单次真实 API/队列验收脚本（重新执行需要新的输出目录，避免覆盖记录）。
- `work/check_recommendation_report_20260927.py`：只读结构与资源检查。
- `work/live-recommendation-report-20260927/run.json`：请求状态、任务、版本与重启结果。
- 同目录 `acceptance.json`、`math-check.json`、`table-source-check.json`：结构、公式及主表对照结果。
- 同目录 `report-before-formula-fix.html`、`math-before.json`、`math-test-red.log`：失败证据。
- 同目录 `tests.log`：全量回归日志。
- 生成报告：`work/live-recommendation-report-20260927/library/2026-09-27/reports/2609.25270-v1-agentict2i-ruler-instance-aware-rubric-rewards-for-sv-bbaaf0664e/report.html`。
