# arXiv Research Assistant

面向 AI 研究者的本地优先科研助手：按时间范围抓取 arXiv 新论文，依据研究兴趣排序并发送每日推荐；需要深读时，再按 arXiv ID 主动生成中文阅读报告和方法图。

当前版本：**v1.2.0**。发布说明见 [`RELEASE_NOTES_v1.2.0.md`](RELEASE_NOTES_v1.2.0.md)，历史版本变更统一记录在 [`CHANGELOG.md`](CHANGELOG.md)。

开发与维护请先阅读 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；正式打包按
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) 逐项核对。

## 已实现能力

- 按 arXiv 类别和最近 N 天检索；
- 正向/负向关键词低成本预筛；
- 可选 LLM 精排，输出推荐分数和理由；
- OpenAlex + Semantic Scholar + arXiv `journal_ref` 元数据核验；
- Semantic Scholar 自动按免费 API 限额节流，遇到 429 时遵循 `Retry-After` 并退避重试；
- 区分 arXiv 首发日期、修订日期和正式发表日期；
- 作者机构、会议/期刊、DOI、引用数及来源冲突记录；
- 方法图优先从 arXiv HTML 的 `figure/img/figcaption` 获取完整原图，缺失时回退到 PDF 版面重建；
- 视觉模型观察图片并生成通俗中文解读，不在报告中照搬原始 caption；
- Docling 可用时自动优先使用，否则回退 PyMuPDF；
- 分片阅读全文并生成结构化中文报告；
- 报告公式通过本地 KaTeX 渲染，离线查看时不依赖 CDN；
- HTML 报告提供桌面侧边目录和移动端可折叠目录；
- 明确区分“作者陈述的局限性”和“分析者推断”；
- Markdown、HTML、JSON及方法图本地归档；
- QQ SMTP 每日推荐邮件、Windows Task Scheduler 和 GitHub Actions 定时运行；
- 每日推荐与完整阅读报告解耦，定时任务不会自动下载和深读全文。
- 可根据关键词与参考 arXiv 论文生成多个研究方向档案，并在 GUI 中随时切换；定时任务自动跟随当前方向。
- Zotero 10+ 本地 API 联动：单篇或批量保存推荐、条目去重、研究方向标签、摘要笔记、PDF 导入和阅读报告附件。
- Obsidian 本地知识库联动：每日推荐、论文笔记、报告图片、周报、研究方向、概念页和 MOC 索引。
- 按研究方向隔离的“我的文献库”：从每日推荐或本地报告显式收藏论文，长期保留摘要精要、推荐依据与报告入口。
- 后台任务支持 1–8 路可配置并行（默认 3）；相同目标的重复任务自动合并，Obsidian 写入和 Semantic Scholar 限流保持并发安全。
- Web UI 支持 1080p、2K、4K 与窄窗口响应式布局；4K 大屏会扩展主画布和工作区高度，并适度放大列表与详情内容。
- arXiv API 检索对连接重置、超时、限流和临时服务错误执行指数退避重试，偶发网络瞬断不会直接中止每日推荐。
- 刷新任务与推荐页面解耦：模型生成中文摘要时不持有缓存锁，新结果完成后再原子发布，因此任务运行期间仍可浏览上一份推荐和历史日期。
- 提供多尺寸 Windows ICO 与透明 PNG 应用图标，并作为 GUI favicon 和品牌标志使用。
- 精简的双信号偏好闭环：加入文献库即视为相关正样本；“不相关”作为负样本，自动影响后续推荐排序。
- 文献推荐页提供“每日记录”月历，可回看当前研究方向每个有结果日期的完整推荐列表；有结果日期会高亮并标注篇数，历史日期的批量 Zotero 保存也只处理当日论文。
- 每周研究综述：结合近 7 天推荐、用户反馈和完整阅读报告，归纳趋势、方法簇、分歧、开放问题与必读清单。
- arXiv 版本追踪：从本地报告、反馈和 Zotero 建立基线，发现 v2/v3 后生成方法、实验、结论与局限差异报告。
- Semantic Scholar 相关工作地图：展示关键参考、后续引用和语义相似论文。

会议状态不会根据 PDF 样式或模型印象猜测。只有得到外部元数据或 arXiv `journal_ref` 支持时才填写，否则保留“未核实”。

## 1. 安装

需要 Python 3.11 或更新版本。Windows PowerShell：

```powershell
cd C:\path\to\arxiv-research-assistant
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

如果研究方向是 AgenticT2I，可直接使用已经过实际检索调优的脱敏模板：

```powershell
Copy-Item config.agentict2i.example.yaml config.yaml
```

该模板使用 45 天滚动候选池，并要求论文同时命中 Agent/LLM/VLM 与视觉生成/编辑两条概念轴；每个方向独立的状态文件会防止每日重复推荐。

如果希望提升矢量图、复杂布局和图注关联效果：

```powershell
python -m pip install -e ".[docling,dev]"
```

Docling 较大，CPU 环境也可以先使用默认的 PyMuPDF 回退链路。

## 2. 配置与切换研究方向

推荐在 GUI 的“方向”页面创建研究方向：输入方向名称、若干关键词和/或参考论文 arXiv ID，系统会读取参考论文元数据，并让已配置的 LLM 归纳 arXiv 类别、服务端查询词、正负关键词与概念组。若 LLM 暂不可用，则使用关键词和参考论文类别生成可用的回退配置。

方向档案单独保存在：

```text
profiles/
├── active.txt
├── agentict2i.yaml
└── another-topic.yaml
```

`config.yaml` 继续保存模型、API 环境变量名、PDF、邮箱等全局设置；`profiles/*.yaml` 只保存检索和排序信息，不包含 `.env` 中的密钥。切换后，GUI、命令行和 Windows 定时任务都会读取 `active.txt`，无需重新安装计划任务。

也可以从命令行查看与切换：

```powershell
arxiv-ra --config config.yaml profiles
arxiv-ra --config config.yaml activate agentict2i
```

配置中心里的“检索”和“排序”字段只修改当前方向，其余字段仍修改全局 `config.yaml`。旧版本首次启动 GUI 或 CLI 时，会自动把 `config.yaml` 中现有的检索和排序参数迁移为默认档案。

### 手工配置研究兴趣

编辑当前的 `profiles/<方向ID>.yaml`（下列内容位于该文件的 `discovery` 段）：

```yaml
discovery:
  lookback_days: 2
  recommendation_count: 5
  min_score: 0.5
  arxiv_categories: [cs.AI, cs.LG, cs.CL, cs.CV, cs.RO]
  positive_keywords:
    - large language model
    - multimodal agent
    - reasoning
  negative_keywords:
    - survey
```

建议将 `positive_keywords` 写得比“AI”更具体，例如方法名称、任务、数据模态和你关注的问题。负向关键词表示降低优先级，不是绝对排除；最终分数低于 `min_score` 的论文不会进入每日推荐。

同时把 `metadata.openalex_email` 改成自己的联系邮箱。OpenAlex 公共 API 可以不填写 key，但填写免费 key 后限额更稳定。

## 3. 配置模型

### OpenAI 或其他兼容服务

在 `.env` 中设置：

```dotenv
LLM_API_KEY=your-key
LLM_BASE_URL=
```

并在 `config.yaml` 中选择账户可用的模型。项目使用 OpenAI-compatible Chat Completions，因此也可以接入其他兼容服务。

### 本地 Ollama

先在 Ollama 中准备一个指令遵循能力较好的模型，然后设置：

```dotenv
LLM_API_KEY=ollama
LLM_BASE_URL=http://localhost:11434/v1
```

将 `llm.model` 改成 Ollama 中的模型名。小模型容易遗漏实验数字或不遵守“未核实”约束，建议使用能力较强的模型。

## 4. 运行

### 本地可视化界面（推荐）

安装 GUI 依赖：

```powershell
python -m pip install -e ".[gui,dev]"
```

启动仅限本机访问的界面：

```powershell
.\scripts\start_gui.ps1
```

也可以直接运行：

```powershell
arxiv-ra --config config.yaml gui
```

浏览器会打开 `http://127.0.0.1:8000`。GUI 提供今日推荐、后台生成完整报告、本地报告库、完整运行配置与服务状态。配置中心可以修改所有 `config.yaml` 字段，也可以更新 `.env` 中的 LLM/OpenAlex/Semantic Scholar API、QQ 邮箱和 SMTP 授权码。

后台并行数可在“配置 → 常规设置”中修改，保存后立即生效：

```yaml
jobs:
  max_parallel: 3  # 允许 1–8，建议 2–4
```

Windows 图标位于 `assets/arxiv-research-assistant.ico`，透明高清源图位于 `assets/arxiv-research-assistant.png`。

安全规则：现有凭据永不回显；凭据输入框留空表示保持原值，只有显式勾选“清除”才会删除。配置页面禁止浏览器缓存并拒绝跨站修改请求。GUI 固定监听 `127.0.0.1`，原有 CLI 与定时任务保持可用。修改 `output_dir` 后需要重启 GUI，以重新挂载本地报告目录。

### 命令行

先做环境检查：

```powershell
arxiv-ra --config config.yaml doctor
```

生成完全离线的示例日报，不下载论文也不调用模型：

```powershell
arxiv-ra --config config.yaml demo
```

执行每日流程：

```powershell
arxiv-ra --config config.yaml run
```

只生成真实推荐、暂不发送邮件（适合调试排序）：

```powershell
arxiv-ra --config config.yaml run --force --no-email
```

为指定论文生成报告：

```powershell
arxiv-ra --config config.yaml report 2409.13740
```

默认结果保存在：

```text
run/
├── state-agentict2i.json
└── YYYY-MM-DD/
    ├── candidates.json
    ├── recommendations.json
    ├── index.html
    └── reports/
        └── ARXIV-ID-title/
            ├── paper.pdf
            ├── report.md
            ├── report.html
            ├── metadata.json
            ├── main-figure.png
            └── figures/
```

`state-<方向ID>.json` 防止相邻时间窗重复推荐同一论文，不同研究方向的去重历史互不影响。`reports/` 仅在主动执行 `report ARXIV_ID` 后产生。需要重新发送同一时间窗的推荐时使用 `run --force`。

## 5. 定时运行

### Windows

以当前用户创建每天 08:00 的任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -ProjectDir $PWD -At "08:00"
```

脚本只创建或更新名为 `arXiv Research Assistant` 的当前用户计划任务。`.env` 和 `config.yaml` 保留在本机。

### GitHub Actions

仓库包含 `.github/workflows/daily.yml`，默认北京时间工作日 07:30 运行，并上传 `run/` 作为私有 workflow artifact。使用前：

1. 将本项目推送到私有 GitHub 仓库；
2. 提交已编辑的 `config.yaml`；
3. 在 Actions Secrets 中添加 `LLM_API_KEY`，以及可选的 OpenAlex、Semantic Scholar、`QQ_EMAIL` 和 `SMTP_PASSWORD`；
4. 启用 Actions。

不要把 `.env` 提交到仓库。

工作流会使用私有 Actions Cache 在不同运行之间保留论文去重状态、阅读反馈、版本基线、周报和每日推荐索引；缓存不是长期备份，本地 `run/` 仍是主数据来源。

## 6. QQ 邮箱投递

项目已经按个人 QQ 邮箱预配置：

```yaml
delivery:
  email_enabled: true
  smtp_host: smtp.qq.com
  smtp_port: 465
  smtp_ssl: true
  email_address_env: QQ_EMAIL
  password_env: SMTP_PASSWORD
```

登录 QQ 邮箱网页版，进入“设置 → 账号 → POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务”，开启 POP3/SMTP 或 IMAP/SMTP 服务并生成授权码。授权码不是 QQ 登录密码。

只在本地 `.env` 中填写以下两项：

```dotenv
QQ_EMAIL=你的完整QQ邮箱地址@qq.com
SMTP_PASSWORD=QQ邮箱生成的授权码
```

同一个 `QQ_EMAIL` 默认同时作为 SMTP 登录帐号、发件人和收件人，因此日报会从你的 QQ 邮箱发送给自己。需要发送给多个地址时，可以在 `config.yaml` 的 `to_addresses` 中显式填写。

配置后先发送独立测试邮件：

```powershell
arxiv-ra --config config.yaml test-email
```

收到标题为“arXiv Research Assistant｜QQ邮箱配置测试”的邮件即表示配置成功。每日正式运行只发送一封“arXiv 每日推荐”邮件，包含入选论文的标题、作者、会议核验结果、推荐理由、摘要、分数及 arXiv/PDF 链接。

定时任务不发送、也不自动生成完整阅读报告。需要深读某篇论文时主动执行：

```powershell
arxiv-ra --config config.yaml report 2409.13740
```

## 7. Zotero 联动

项目通过 Zotero 10+ 的本地 API 写入个人文献库，不需要把 Zotero 云端 API key 交给本项目。首次使用：

1. 启动 Zotero；
2. 在“设置 → 高级”中启用“允许本机其他应用与 Zotero 通信”；
3. 打开 GUI 的“配置 → Zotero”；
4. 点击“连接并授权”，然后在 Zotero 弹窗中批准 `arXiv Research Assistant` 写入。

授权 key 自动保存到本机 `.env` 的 `ZOTERO_LOCAL_API_KEY`，不会显示在网页或进入脱敏压缩包。

可以从今日推荐页保存当前论文或批量保存今日推荐，也可以从报告库把完整阅读报告附加到同一 Zotero 条目。每次保存前都会弹出位置选择器，实时读取 Zotero 的现有分类树；可以选择任意嵌套分类、使用默认分类，或只放在“我的文库”中。已有论文会加入新选择的分类，但不会从原有分类移除。

创建前会按 DOI、arXiv ID 和标题检查现有条目；重复点击不会重复创建论文。默认分类为 `arXiv Research Assistant`，并添加当前研究方向与 arXiv 类别标签。

PDF 支持两种模式：

- `imported_file`（默认）：下载 arXiv PDF 并导入 Zotero 存储，可随 Zotero 文件同步；
- `linked_file`：只链接项目中的本地文件，不占用 Zotero 存储，但换电脑后路径可能失效。

阅读报告 HTML 默认使用本地链接附件，避免重复占用 Zotero 存储。相关配置：

```yaml
zotero:
  enabled: true
  mode: local
  base_url: http://127.0.0.1:23119/api
  api_key_env: ZOTERO_LOCAL_API_KEY
  collection_name: arXiv Research Assistant
  attach_pdf: true
  pdf_attachment_mode: imported_file
  attach_report: true
  add_profile_tag: true
```

## 8. 阅读反馈与研究周报

今日推荐只保留两种明确的偏好动作：点击“加入文献库”即把论文视为相关正样本；点击“不相关”则保存为负样本。两种状态互斥，收藏会清除该论文的负反馈，标记不相关会将论文移出当前方向的文献库。负样本按方向写入 `run/feedback-<方向ID>.json`，正样本直接来自 `run/paper-library-<方向ID>.json`；系统从标题提取可审计的偏好信号，命中相同短语或多个特征词的近似论文会在候选阶段被排除，并且不会静默改写 `profiles/*.yaml` 中的手工关键词。

首页推荐也按当前研究方向读取各自最近一次结果。为避免文献库持续增长后对排序产生过强影响，推荐学习只使用当前方向最近收藏的 30 篇论文作为正样本。“加入文献库”同时出现在推荐页、报告列表和完整报告阅读页；从报告收藏时会优先复用当前方向的摘要缓存，缺失时从完整报告的“一句话总结/为什么值得阅读”提取中文内容，并在模型可用时补齐为与推荐页相同规格的摘要精要和保存价值。文献库页面会自动修复历史不完整条目，并根据本地报告状态显示“打开论文报告”或“生成论文报告”，同时继续支持搜索、arXiv/PDF、Zotero 与移出操作。

周报可以在“周报”或“任务”页面手动生成，也可以由每日任务在指定星期自动生成。默认输出位于：

```text
run/weekly/YYYY-Www/
├── report.md
├── report.html
└── metadata.json
```

配置示例：

```yaml
weekly:
  enabled: true
  days: 7
  max_papers: 50
  include_deep_reports: true
  auto_generate: true
  weekday: 6  # 0=周一，6=周日
```

也可以从命令行随时生成：

```powershell
arxiv-ra --config config.yaml weekly
```

## 9. arXiv 版本追踪与相关工作地图

版本追踪会合并当前方向的本地报告、阅读反馈和 Zotero 条目，并使用一次批量 arXiv API 请求建立或刷新基线。首次检查只记录当前版本，不会误报更新。检测到版本号上升后，系统才下载新旧 PDF，抽取关键章节并生成差异报告。

```text
run/
├── version-state-agentict2i.json
└── versions/
    ├── index.html
    └── ARXIV-ID/v1-to-v2/
        ├── report.md
        ├── report.html
        ├── ARXIV-IDv1.pdf
        └── ARXIV-IDv2.pdf
```

PDF 下载会跳过已有文件，检查最小文件大小，连续下载间隔至少 1 秒，并在 429 后等待重试。每日任务默认自动刷新版本基线，也可以手动运行：

```powershell
arxiv-ra --config config.yaml versions
```

“图谱”页面可根据一个 arXiv ID 生成 Connected Papers 风格的交互式关系画布：左侧论文列表、中心力导向 SVG 图、右侧详情面板。主布局不是引用树，而是使用图内论文标题与摘要的 TF-IDF 相似度；相似度越高，连线越粗、距离越近。节点大小表示引用量，暖灰到炭灰的颜色表示年份，橙色节点表示起点论文。支持搜索、关系筛选、最低引用数、标签开关、缩放、平移、拖拽节点与点击定位。

图谱候选集包含关键参考、后续引用和 Semantic Scholar 推荐论文；真实引用边通过一次批量请求补齐，但默认不参与布局，可在工具栏中作为橙色虚线叠加层显示。页面内的“如何阅读”会说明本项目的相似度近似与 Connected Papers 专有嵌入算法之间的差别：

```powershell
arxiv-ra --config config.yaml citation 2407.05600
```

结果保存在 `run/citations/ARXIV-ID/`。引用关系来自 Semantic Scholar，可能存在元数据同步延迟，应以论文原文为准。

## 10. Obsidian 知识库联动

项目可以把现有本地成果同步为 Obsidian 原生 Markdown 知识库。它不依赖 Obsidian 插件或云端 API，只需要一个已经存在的 vault 路径。

推荐通过 GUI“配置 → Obsidian”选择 vault。对于专用研究 vault，可把 `root_folder` 设为 `.`，形成下面的可发现性分层；若 vault 还用于其他用途，则保留一个独立根目录即可：

```text
ObsidianVault/
├── Home/
│   └── Research Hub.md
├── Daily/YYYY/MM/
├── Papers/<研究方向>/
├── Topics/
├── Reviews/Weekly/YYYY/
├── Attachments/
└── System/
    ├── Profiles/
    └── Indexes/
```

笔记包含 YAML frontmatter、`[[双向链接]]`、概念反向链接和 MOC 索引。论文文件名优先使用标题冒号前的方法/模型名（例如 `ToolArtist.md`、`VCode.md`）；没有明确方法名时使用可读的论文标题。arXiv ID 只保存在 frontmatter 和正文链接中，不再占用文件名。论文正文不写入冗长的 arXiv 原文摘要，而是优先复用完整报告的模型“一句话总结”，其次使用模型推荐理由，必要时调用模型生成 3–5 句中文精炼摘要并缓存。完整报告中的本地方法图会复制到 `Attachments/` 并改写成 Obsidian wikilink；PDF 默认不复制，仍由 Zotero 管理。

为了保护个人编辑，应用只更新托管标记内的内容：

```html
<!-- ARXIV_RA_MANAGED_START -->
应用生成内容
<!-- ARXIV_RA_MANAGED_END -->
```

标记以外的“我的笔记”“我的周总结”等内容不会被覆盖。若同名文件已经存在但不含托管标记，系统会使用带 `(arXiv RA)` 的备用名称，不会修改原文件。同步过程幂等，内容没有变化时不会重写；不会自动 git commit 或 push。

配置示例：

```yaml
obsidian:
  enabled: true
  vault_path: 'D:\YourVault'
  root_folder: .
  home_folder: Home
  daily_folder: Daily
  papers_folder: Papers
  concepts_folder: Topics
  weekly_folder: Reviews/Weekly
  attachments_folder: Attachments
  profiles_folder: System/Profiles
  indexes_folder: System/Indexes
  auto_sync: true
  sync_daily: true
  sync_reports: true
  sync_weekly: true
  sync_feedback: true
  copy_figures: true
  copy_pdf: false
  generate_concepts: true
```

手动执行全量同步：

```powershell
arxiv-ra --config config.yaml obsidian-sync
```

目录、frontmatter、双向链接和 MOC 思路参考了 [huangkiki/dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)，但实现使用本项目已有的数据模型、报告结构、研究方向与安全边界，没有直接访问 Zotero SQLite，也不会自动删除用户笔记。

生成的完整报告、方法图和 PDF 仅保存在本地 `run/YYYY-MM-DD/reports/`。

报告结构约定：完整报告固定使用“基本信息 → 总结与价值 → 问题背景 → 核心方法 → 贡献 → 实验与结果 → 对比 → 局限 → 可复现性”的顺序。不再武断指定唯一主图；实验章节之前能够可靠定位的 Figure 仅在“核心方法 → 方法图解析”出现一次。图片下方不展示 `arXiv HTML`、`PDF 第 N 页` 等内部来源标签。图示解读由定稿器统一生成，报告模型不得再创建第二套 Figure 小节。核心方法正文需另外说明整体数据流、关键模块、训练/推理差异；若论文包含关键公式，则逐式解释符号、计算顺序、设计动机和对训练或推理的作用。无法可靠提取整图时直接跳过，不生成占位章节。报告不包含“阅读建议”和“核验备注”。修改渲染规则后，可批量规范化并重建本地报告：

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\rebuild_reports.py .
```

需要重新调用视觉模型为方法图生成通俗解读时：

```powershell
python scripts\rebuild_reports.py . --paper 2511.02778 --explain
```

## 报告准确性边界

- arXiv `published` 是预印本首次公开时间，不等同正式出版时间；
- Semantic Scholar/OpenAlex 可能存在同步延迟或元数据冲突，冲突会写入报告；
- arXiv HTML 不存在、转换失败或某个 Figure 由多个不明确图片节点组成时，该 Figure 会回退到 PDF；
- PDF 回退通过 caption 位置与同页位图/矢量对象的联合边界重新裁剪，避免误取复合图中的单个嵌入式子图；
- LLM 生成内容必须作为研究助理初稿使用，关键数值和结论应回看 PDF。

## 测试

```powershell
python -m pytest
python -m compileall -q src tests
```
