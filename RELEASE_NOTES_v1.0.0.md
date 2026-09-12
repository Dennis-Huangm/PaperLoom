# arXiv Research Assistant v1.0.0

发布日期：2026-08-19

## 定位

v1.0.0 是第一份正式版，面向以 arXiv 为主的 AI 研究工作流，覆盖“方向配置 → 每日发现 → 阅读反馈 → 深度报告 → 周报 → 版本追踪 → 相关工作图谱 → Zotero 归档”。项目采用本地优先设计，完整报告和论文文件默认只保存在本机。

## 正式版能力

- 多研究方向档案与即时切换；
- 每日推荐、按方向去重和 QQ 邮件摘要；
- LLM 精排与可审计的阅读反馈加减分；
- OpenAlex、Semantic Scholar、arXiv 出版信息核验；
- 中文完整阅读报告、KaTeX 公式和方法图通俗解读；
- Zotero 分类选择、条目去重、PDF 和报告附件；
- 每周跨论文研究综述；
- arXiv 新版本监控与 PDF 差异报告；
- 相关工作交互图谱：相似度布局、引用叠加、年份颜色、引用量节点大小、全标签避碰；
- GUI、CLI、Windows Task Scheduler 和 GitHub Actions。

## 安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "arxiv_research_assistant-1.0.0-py3-none-any.whl[gui]"
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
arxiv-ra --config config.yaml doctor
arxiv-ra --config config.yaml gui
```

从源码目录开发或运行测试：

```powershell
python -m pip install -e ".[gui,dev]"
python -m pytest -q
```

## 已知边界

- Semantic Scholar 免费 API 可能返回 429；系统会限速、退避并尽量复用上次图谱缓存。
- 图谱相似度使用当前节点标题与摘要的本地 TF-IDF 近似，不等同于 Connected Papers 的专有嵌入算法。
- 首次版本追踪只建立基线；只有之后检测到版本号上升才生成差异报告。
- 作者机构、会议、引用数和 DOI 受外部数据库同步延迟影响；不确定信息保留“未核实”。
- Zotero 写入需要 Zotero 10+、本地 API 开启及首次用户授权。
- Docling 为可选依赖；未安装时自动使用 PyMuPDF。
- GitHub Actions Cache 用于跨运行延续状态，但不应视作长期备份。

## 发布验证

- Python 3.11+；
- 56 项自动化测试通过；
- Python、Jinja、YAML 和 JavaScript 语法检查通过；
- CLI doctor、研究方向列表和依赖一致性检查通过；
- 正式包执行敏感文件排除和凭据内容扫描；
- wheel 在隔离环境完成安装与导入烟雾测试。
