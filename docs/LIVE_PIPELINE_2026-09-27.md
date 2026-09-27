# 单篇报告自动化真实试运行

结论：一次真实的单篇报告队列任务已完成官方 API 获取、PDF 下载、正文及图片解析、真实模型图片解读/分片/整合、质量检查和 Markdown/HTML 保存。运行发现 LaTeX 表头箭头误判，修复后以同一输入快照和模型检查点再走正式生成流程，零新增模型请求，最终任务成功且无任务警告。此次没有验收定时触发、每日选题、邮件、Zotero 或 Obsidian。

## 实际运行

- 论文：VectorEdits，`2506.15903v1`，5 页。独立输出目录 `work/live-pipeline-proxy-20260927/`；未向个人报告库写入。
- 正式入口：`JobManager.submit` → `DailyPipeline.report_arxiv_id`。首次没有快照、缓存 PDF 或预设模型输出；实际 arXiv API 元数据来源无回退说明。
- 保留项目 PDF/分片配置：`use_docling_if_available=true`，本次实际使用 Docling；上限 60 页、分片 18000 字符。解析了全部 5 页，2 个正文分析分片。
- 请求模型 `gemini-3.8-flash-high`，服务返回 `gemini-3.8-flash`。凭据仅临时环境变量，不写脚本/配置/运行记录。
- 共 8 次实际请求：4 张图片解读，其中一张首次 180 秒超时、自动兼容重试成功；2 次正文分析；1 次最终整合。7 次成功，1 次超时。SDK 隐式重试关闭，请求预算 16，未触及预算。
- 首次联网任务耗时约 7 分 35 秒，记录为 `succeeded_with_warnings`；警告来自下述检查误判。原始产物和运行记录保留，不改写成无警告成功。
- 任务关闭后重新加载，任务状态和结果记录保持一致。

## 网络诊断与用户澄清

初始 API 查询出现 HTTP 406。早期诊断沿用了离线测试的清除代理命令；用户指出其 Clash Verge 已为 arXiv 配置出口。随后保留 HTTP/HTTPS 代理测试，曾短暂复现指定 ID 查询 406，而 electron 查询 200。用户 CMD 同一指定 ID 查询返回 200，紧接着本机 curl.exe 和 Python 原请求头也全部返回 200。

这些证据不能确定临时 406 的具体原因，也不能认定用户代理失效。曾添加的官方摘要页回退已撤回，`arxiv_client.py` 没有本轮业务改动；原型移至 `work/arxiv-network-diagnostic-20260927/`，不参与生产或测试。

当前环境的 ALL_PROXY 为 SOCKS，但 Python 未安装 socksio。在线试运行仅在进程启动时移除 `ALL_PROXY/all_proxy`，保留指向 Clash `127.0.0.1:7897` 的 HTTP_PROXY/HTTPS_PROXY。没有修改系统/Clash/项目配置或安装包；离线测试仍使用清除代理的测试命令。

## 真实输出发现的误判与修复

源表头使用 Unicode `↑/↓`，模型使用合法的 `\(\uparrow\)/\(\downarrow\)`。`table_quality._header` 原先将两种写法判为不同限定标记，导致 7 行共 28 个数值单元格误隐藏。

新增测试先复现失败，再仅将完整的行内 LaTeX 箭头（`\(...\)` 或 `$...$`）归一为 Unicode 方向符号。不进行任意公式/单位换算。正确方向保留，真实反向箭头仍判为冲突。

修复验证分两次，均禁止新增模型调用：

1. 首次重新走生产报告入口并重新核验外部元数据，已复用图片与两段笔记，但整合提示词发生变化，旧整合结果被正确拒绝。因请求预算为零，生成摘要级回退。记录 `post-fix-replay.json` 为 `incomplete`，不算验收成功。
2. 固定首次运行保存的 `VerifiedMetadata`，仍使用生产报告生成入口和经过签名/文件哈希验证的检查点。全部复用，0 模型请求、0 被阻止请求，最终 `succeeded`，无任务警告。记录 `post-fix-frozen-replay.json`。这是固定输入回放，不是第二次从网络获取全部材料。

最终报告：

- `report_quality=full`，4 张图片均有解读，HTML 中有 4 个图片元素，所有目标文件存在。
- 3 张表，28 条已定位引用，68 个正文 PDF 引用链接；仍有 1 条无法定位的引用，保持未核验状态。
- 18 个数值块，本规则下 0 个问题、0 个隐藏单元格；基线行数值 `0.9634 / 0.9011 / 10488 / 0` 完整保留。
- 队列重新加载后最终成功状态与结果一致。

这些数字只说明运行、结构和当前规则的检查结果，不是语义真实性或全文正确率。元数据机构/发表信息、作者结论与报告概括仍需人工核对。本轮没有新增浏览器视觉验收；图片检查为生成 HTML 和本地文件完整性检查。

## 文件与验证

- 首次真实运行：`work/live-pipeline-proxy-20260927/run.json`。
- 最终固定输入回放：同目录 `post-fix-frozen-replay.json`。
- 执行脚本：`work/run_live_pipeline_20260927.py`、`work/replay_live_pipeline_fix_20260927.py`。在线脚本需凭据环境变量；报告脚本要求新输出目录，不覆盖旧记录。
- 最终报告：`work/live-pipeline-proxy-20260927/library/2026-09-27/reports/2506.15903-v1-live-pipeline-qa-vectoredits-a-dataset-and-benchmark-for-in-23dde2ea2b/report.html`。
- 最终回归：**502 passed, 1 warning in 39.04s**，日志 `work/live-pipeline-final-tests.log`。唯一警告为原有 Starlette TestClient/httpx 弃用提示。`git diff --check` 通过。

本批生产改动只有表头 LaTeX 箭头等价识别及相应回归测试。实际使用时需让运行进程加载新代码，并保持可用代理/模型连接。定时任务触发和外部发送未在本轮执行，不能据此承诺所有自动化场景永不失败。
