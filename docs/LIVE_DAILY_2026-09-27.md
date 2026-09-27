# 每日推荐与启动环境验收

本轮完成了两次真实每日推荐运行、跨批次去重和历史报告入口检查；没有完成“每日检索到新深度报告”的全链路验收，也没有安装或触发 Windows 定时任务。

## 真实日报

- 沿用当前 AgenticT2I 方向的兴趣、45 天窗口、概念组和相关性阈值，只在独立验收配置中缩小规模：arXiv 候选上限 60、alphaXiv 上限 5、预筛 6、每批推荐 2。
- 输出为 `work/live-daily-env-20260927/library/`。未改动个人方向、实际配置和个人报告库。关闭邮件、Zotero、Obsidian、自动周报与版本追踪/同步。
- 使用正式 `JobManager` → `DailyPipeline.run(deliver=False)`，连续运行两次，不使用 demo、不强制忽略历史。模型使用用户指定的 Gemini 配置，通过临时环境变量传入凭据。
- 每轮实际模型请求 2 次，分别用于候选精排与中文摘要/推荐依据，共 4 次；每轮请求上限 6，未触及。没有发生深度报告模型请求。
- 两轮各保存 2 篇，共 4 篇，无跨批次重复；当日日报合并保留 4 篇。各批次 HTML 包含所选论文标题，中文摘要和推荐依据均已保存。关闭并重新加载队列后记录一致。
- 两轮状态均为 `succeeded_with_warnings`：arXiv 组合查询返回 HTTP 406，alphaXiv 成功回退；arXiv 补全延后，4 篇均为 partial 元数据、无可靠版本号。候选来源和失败信息保留，不能将其称为官方元数据核验通过。

第一次验收脚本未加载项目 `.env`，因此错误地缺失 alphaXiv 凭据并失败。该失败保留在 `work/live-daily-20260927/`，属于验收脚本问题。脚本随后调用正式 CLI 使用的 `_load_dotenv`，在新的目录重新执行。不会将第一次记录改成成功，也不会把测试脚本漏载凭据解释为用户配置错误。

## 后续报告入口

目前每日任务生成推荐日报，不自动为所有推荐生成深度报告。原 CLI 帮助和调度文档容易将两者混淆，本轮修正说明。

使用这 4 条真实推荐，在独立项目副本运行实际 FastAPI 路由：`POST /api/jobs/report`，传入推荐来源和日期。全部返回 HTTP 409，提示历史快照没有可靠版本号，应在生成页面获取最新版本或输入指定版本号。没有提交报告任务，也没有额外模型调用。这是正确的版本保护，不是成功生成了报告。

另行尝试通过正式 `ArxivClient.get_many` 解析这 4 个 ID，也遇到 HTTP 错误，未获得可用版本。因此此次没有挑选其他论文替代，也没有把上轮单篇报告验收当成本轮推荐后续报告成功。

## 网络证据与边界

在线测试保留 HTTP/HTTPS 代理，仅移除当前 Python 不支持的 `ALL_PROXY/all_proxy` SOCKS 配置。没有改变 Clash、系统环境或实际 `.env`。

同一失败组合查询的后续对照记录：Python 首次仍为 406，electron 对照为 200，curl 同一组合查询为 200；之后 Python 使用生产请求头及 curl 风格请求头都为 200，而 curl 使用生产请求头的一次请求发生 TLS 错误（退出码 35）。这说明响应会变化，不能据此把故障确定归因于请求头、Python 或 Clash 出口。此次没有修改 arXiv 查询、放宽日期/相关性过滤、增加未验证的回退或无限重试。

## 启动环境检查与改进

只读检查发现：

- 当前终端 Python 为 `D:\miniconda3\python.exe`，项目 `.venv\Scripts\python.exe` 不存在。
- GUI 启动脚本在没有项目 `.venv` 时使用 PATH 中的 Python；Windows 安装脚本要求项目 `.venv`，不能假设二者使用同一解释器。
- 没有匹配 PaperLoom/arXiv/`arxiv_ra` 的已注册 Windows 计划任务。项目调度默认关闭，未设置方向计划。本轮未改变这些状态。
- 项目正式配置的模型与本轮 Gemini 临时验收配置不同。因此临时验收成功不表示正式定时任务已经采用测试模型。
- 继承环境包含 SOCKS `ALL_PROXY`，当前 Python 没有 `socksio`，会导致 HTTP 客户端初始化失败。

`doctor` 新增不联网的 HTTP 客户端初始化检查，检查实际代理和证书环境。依赖缺失、无效代理或证书配置会返回非零退出码；错误信息不回显可能含凭据的代理 URL。SOCKS 依赖缺失时给出在同一 Python 环境安装 `"httpx[socks]>=0.27,<1"` 的提示，不自动删除代理配置。

当前机器验证：继承原环境时 `doctor` 返回 1；仅移除 SOCKS ALL_PROXY、保留 HTTP/HTTPS 代理时返回 0。该检查明确说明：不验证外部服务可达性、模型响应或定时触发。

## 记录与后续

新增 3 条诊断回归测试先复现失败，再通过修复。最终全量 **505 passed, 1 warning in 71.77s**（`work/daily-final-tests.log`）；唯一警告为已有 Starlette TestClient/httpx 弃用提示。`compileall` 与 `git diff --check` 通过。

- `work/live-daily-env-20260927/run.json`：两轮状态、模型请求次数、检索与推荐批次。
- 同目录 `handoff-api.json`、`report-handoff-resolution.json`、`artifact-checks.json`：入口保护、解析尝试和生成文件检查。
- `work/live-daily-20260927/network-probe*.json`：请求对照，不包含凭据。
- `work/daily-doctor-inherited.log`、`work/daily-doctor-http-proxy.log`：启动环境检查。
- 本轮没有新增桌面或窄屏视觉验收；HTML 与文件检查不能替代视觉验收。

投入定时使用前，应先统一 GUI/计划任务的 Python、模型和代理环境，再补一次 arXiv 元数据可用条件下的“新推荐 → 指定版本报告”验收，最后配置所需的计划时间与触发入口。
