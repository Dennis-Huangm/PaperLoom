# 日常推荐定时入口验收

日期：2026-09-27。范围是项目调度器和 Windows CLI 入口，未注册或启用 Windows 系统计划任务。

## 方法

从正式配置加载研究方向、模型与代理，使用项目 `.venv`。在独立项目中复制非秘密配置，关闭邮件、Zotero、Obsidian、自动周报、版本追踪/同步；将本次检索限制为 arXiv 60 个候选、预筛 6 篇、推荐 2 篇、alphaXiv 最多 5 个候选。模型保留 `gemini-3.8-flash-high`。

正式 `.env` 仅加载进 QA 父进程环境供子进程继承，没有复制到隔离目录。正式配置、活动方向和正式调度文件通过前后哈希检查保护。本轮验证的是继承该环境的启动器子进程，不能当作已验证 Windows Task Scheduler 服务的环境。

验收脚本 `work/live_schedule_acceptance_20260927.py` 在真实墙上时钟的下一个测试时刻配置计划，使用独立 PowerShell 进程执行正式的 `scripts/run.ps1 -ConfigPath <隔离配置> schedule --once`，不修改调度器时钟，不替换模型或网络响应。启动器从项目外目录运行。

顺序为：到点前检查→到点执行→同时启动第二进程→退出后同日重启。最后关闭隔离目录的调度开关。

## 首轮失败与保护行为

09:33 首轮按时登记一个任务 `71507a7e86de`，但实际检索失败：arXiv 连接失败且用尽 3 次重试，alphaXiv 返回 TLS `UNEXPECTED_EOF_WHILE_READING`。这是连接错误，不是此前的 HTTP 406。

- 到点前没有任务。
- 并发第二进程识别到目录已被队列占用，没有重复提交。
- 实际失败返回退出码 1，调度历史保留 `failed` 和错误详情，没有发布空推荐结果。
- 在同日重新启用该隔离计划并再次执行，原任务完全保留，没有重试和新增任务；该次无新任务的检查返回 0，不代表历史失败已恢复。
- 后续 curl 固定论文查询返回 HTTP 200；项目 `.venv` 的固定查询返回 1 篇、同类新组合查询返回 60 篇。

这组证据支持当次连接中断可能是暂态故障；没有上游日志，不能确定代理或服务器内部原因。本轮未修改 Clash、TLS、代理或重试策略。失败与成功记录均保留，不能将后来成功解释为从未发生故障。

首轮记录：`work/live-schedule-20260927/`；失败后重启记录：`failed-slot-restart.json`；网络复测：`network-recheck.json`。

## 第二轮成功与 GUI 接续

另建 `work/live-schedule-recheck-20260927/`，使用相同配置和启动器，按真实时刻 09:40 执行。没有改变网络设置，也没有自动重试首轮已消耗的时段。

| 检查 | 结果 |
| --- | --- |
| 09:38 到点前运行 | 无提交、退出码 0 |
| 09:40:00 到点启动 | 提交任务 `0413bf147741` |
| 同时启动第二进程 | 识别队列占用，退出码 0，无重复任务 |
| 真实检索 | arXiv 成功返回 60 篇，alphaXiv 成功返回 5 篇 |
| 日报 | 推荐 2 篇、单个批次，约 09:41 完成；退出码 0，任务 `succeeded`，无警告 |
| 同日新进程重启 | 无提交，原任务完全一致，退出码 0 |
| GUI 接续 | 首页、调度页、任务页、日报均 HTTP 200；调度历史显示原任务，没有新任务 |
| 清理与正式配置 | 两份隔离计划均关闭；正式 `.env`、配置、当前方向和调度文件前后哈希一致 |

两篇实际推荐为 WeAgent-MMGenEdit（2609.05171v1）和 GenRouter（2608.16721v1），都有完整元数据和确定版本。该隔离库没有复制个人历史去重状态，因此可能与用户以前的推荐重复；这不是同一调度时段重复执行。

成功记录：`run.json`；各次独立进程日志：`before-slot.log`、`due-slot.log`、`overlapping-process.log`、`restart-same-day.log`；GUI 检查：`gui-handoff.json`。日报：`library/2026-09-27/index-agentict2i.html`。

## 运行环境与边界检查

从项目外目录调用正式 `scripts/run.ps1 doctor`，确认实际解释器为项目 `.venv/Scripts/python.exe`，配置为根目录 `config.yaml`，环境、代理初始化和可选解析组件检查通过。doctor 不联网，不能代替真实定时执行结果。

定向回归：`tests/test_scheduler.py`、`tests/test_windows_launchers.py`、`tests/test_durable_jobs.py` 共 **34 passed，1 warning，8.22 秒**。包括时区/星期、补触发窗口、并发、失败/中断、重启、恢复副本默认暂停及启动入口检查。唯一警告为已有 Starlette TestClient/httpx 弃用提示。

本轮没有修改生产代码。Windows OS 登录/睡眠唤醒、后台代理可用性、SMTP 投递及长期稳定性不在本次通过范围内。日常推荐不会自动为全部候选生成深度报告。
