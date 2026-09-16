# PaperLoom v1.3.0 · 知织

发布日期：2026-09-16

把论文织成知识。PaperLoom 是原 arXiv Research Assistant 的新名称，本版本将 arXiv 与 alphaXiv 从故障回退关系升级为协作检索，同时完善研究方向隔离、历史论文版本与并发任务行为。

## arXiv 与 alphaXiv 协作

- `hybrid` 模式合并 arXiv 类别与关键词检索、alphaXiv 语义发现的候选，统一去重与排序。
- alphaXiv 候选由 arXiv 补全元数据；发现来源与补全结果随论文保留。
- 保留 `auto` 旧版故障回退模式与 `arxiv` 单源模式，已有配置可继续使用。
- 本地完整元数据缓存支持网络失败后的读取；正式出版信息仍经过独立核验。

## 更可靠的研究记录

- 从历史推荐或文献库生成报告时保留选中的论文版本；手工输入无版本 arXiv ID 查询最新版本，指定 `v2`/`v3` 时严格使用该版本。
- 元数据、PDF 与方法图保持版本一致，报告与周报等输出按研究方向隔离。
- 收藏与“不相关”统一存入 `reading-state-<profile>.json`，通过跨进程锁和原子保存处理并发修改。首次写入会导入旧状态；原文件保留。
- 后台任务使用提交时的配置和研究方向，切换方向不会改变已排队任务的目标。
- 客户端统一延迟创建与清理，改善网络资源生命周期与测试隔离。

## 新名称与升级兼容

- 仓库地址：https://github.com/Dennis-Huangm/PaperLoom
- 新 CLI 命令为 `paperloom`；`arxiv-ra` 继续可用。
- Python 发行包名仍为 `arxiv-research-assistant`，模块名仍为 `arxiv_ra`。
- 现有 `.env`、`config.yaml`、`profiles/` 与 `run/` 可继续使用。原有 Zotero/Obsidian 目录及 Windows 计划任务名称不自动迁移。
- README 重新整理了安装、离线体验、双源检索、配置与日常使用说明。

升级前备份个人配置和运行数据。在更新后的源码目录使用原虚拟环境执行：

```bash
python -m pip install -e ".[gui]"
paperloom --version
```

若使用已有 Git 克隆，将远程地址更新为：

```bash
git remote set-url origin https://github.com/Dennis-Huangm/PaperLoom.git
```

## GitHub Actions 与通知

- 日报改为**仅手动触发**，取消原有工作日定时运行；本地计划任务不受影响。
- 旧工作流因缺少被 Git 忽略的 `config.yaml` 而反复失败。这些失败会产生 GitHub Actions 通知，发生在 PaperLoom 的邮件投递步骤之前。
- 手动运行前，将完整配置存为仓库 Secret `PAPERLOOM_CONFIG_YAML`，并设置所需 API/SMTP Secrets；个人配置无需公开提交。
- 补充 alphaXiv 密钥映射和新状态缓存；报告 artifact 只有勾选上传选项时才保存。
- 公开仓库的 Actions 缓存和产物不能视为私有研究存储。个人数据建议使用本地环境或私有仓库。

## 下载与验证

- `paperloom-1.3.0-source.zip`：源码、文档和测试，适合从源码安装。
- `arxiv_research_assistant-1.3.0-py3-none-any.whl`：保留发行包名的 Python 安装包。
- `SHA256SUMS.txt`：上述两个文件的 SHA-256 校验值。

打包脚本修复了 PowerShell 字节码筛选误删暂存源码的问题，并对归档执行关键文件存在性、个人数据排除与校验值检查。

完整离线测试通过：**153 项**。测试环境存在 1 项已有 Starlette/httpx 弃用提示，不影响测试结果。
