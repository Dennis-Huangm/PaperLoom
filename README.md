<p align="center">
  <img src="assets/arxiv-research-assistant.png" width="104" alt="PaperLoom 标志">
</p>

<h1 align="center">PaperLoom · 知织</h1>

<p align="center">
  <strong>把论文织成知识。</strong><br>
  Weave papers into understanding.<br>
  面向个人研究者的本地优先论文助手。
</p>

<p align="center">
  <img alt="Python 3.11 或更新版本" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-green"></a>
  <a href="https://github.com/Dennis-Huangm/PaperLoom/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/Dennis-Huangm/PaperLoom"></a>
</p>

<p align="center">
  <a href="#quick-start">快速开始</a> ·
  <a href="#daily-use">日常使用</a> ·
  <a href="#upgrade">升级</a> ·
  <a href="docs/USAGE.md">完整指南</a> ·
  <a href="#faq">常见问题</a>
</p>

---

PaperLoom 把**发现论文 → 筛选推荐 → 全文阅读 → 研究记录 → 持续跟进**放在同一个工作流中。arXiv 提供类别与关键词检索，alphaXiv 可补充语义候选；你选择深读的论文，系统生成带原文定位的中文报告，并可连接 Zotero、Obsidian 与邮件。

当前版本 **1.4.0**，提供本地 Web 界面与 CLI。配置、阅读记录和生成文件由你保存在本地；联网检索和远程模型仍会访问所选服务。参见 [本次发布说明](RELEASE_NOTES_v1.4.0.md) 与 [完整变更记录](CHANGELOG.md)。

## 能做什么

| 能力 | 使用方式 |
| --- | --- |
| 按研究方向发现论文 | arXiv / alphaXiv 候选合并、规则评分、可选模型精排，保留推荐依据与筛选记录 |
| 管理阅读与偏好 | 收藏、排除单篇、屏蔽主题词、撤销反馈；记录待读／阅读中／已读、标签、笔记和已读修订版 |
| 生成中文全文报告 | 方法、公式、结果与可提取的方法图，输出 Markdown / HTML，提供固定论文版本的 PDF 页码及原文定位 |
| 汇总与比较 | 研究活动周报；选择 2–5 篇已知修订版论文，按七个维度生成跨论文比较 |
| 搜索本地材料 | 搜索标题、摘要、报告、已解析 PDF 和个人笔记，按研究方向隔离 |
| 追踪新修订版 | 手动或批量检查、预览变化、同步指定版本，保留历史产物 |
| 管理长期运行 | 持久任务历史、显式中断恢复、兼容检查点复用、多方向定时调度 |
| 备份与导出 | 备份校验、恢复预览与独立目录恢复；按需同步 Zotero / Obsidian 或发送邮件 |

报告生成流程包含数值、表格归属与引用覆盖检查，并共用 Markdown、公式和表格渲染器。依据不足的内容会收敛展示，原始内容保留在核对详情中。**这些检查用于减少常见错误，不能替代对原论文的语义核对。**

<a id="quick-start"></a>
## 快速开始

需要 **Python 3.11+**。Windows 的 Python 3.13 环境有经过验收的依赖约束；其他 Python 版本和平台按依赖声明安装，需自行验证。

从 [Releases](https://github.com/Dennis-Huangm/PaperLoom/releases/latest) 下载 `paperloom-1.4.0-source.zip` 并解压，或克隆仓库：

```bash
git clone https://github.com/Dennis-Huangm/PaperLoom.git
cd PaperLoom
```

### Windows / PowerShell

在包含 `pyproject.toml` 的目录运行：

```powershell
# 建立或更新项目独立环境；无需激活
powershell -ExecutionPolicy Bypass -File scripts\setup_environment.ps1

# 首次运行复制模板，不覆盖已有配置
if (-not (Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# 本地配置和依赖检查，然后启动界面
powershell -ExecutionPolicy Bypass -File scripts\run.ps1 doctor
powershell -ExecutionPolicy Bypass -File scripts\start_gui.ps1
```

浏览器入口为 **http://127.0.0.1:8000**。安装脚本、GUI、日常命令与 Windows 计划任务统一使用项目 `.venv`。安装可加 `-WithDocling` 启用可选 PDF 解析器、`-Dev` 安装测试依赖，或用 `-Python "D:\path\to\python.exe"` 指定基础解释器。不要删除创建虚拟环境所依赖的基础 Python。

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[gui]'
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
paperloom --config config.yaml doctor
paperloom --config config.yaml gui
```

默认使用 PyMuPDF，Docling 为可选依赖。Windows 脚本不适用于其他平台；下文通用 CLI 命令均需在已激活环境中运行。

### 配置模型与首次联网

在 `.env` 中填写服务凭据，在 `config.yaml` 的 `llm.model` 中填写该服务提供的模型名称：

```dotenv
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://your-provider.example/v1
```

使用 SDK 默认服务地址时可留空 `LLM_BASE_URL`。不要将个人密钥写入示例配置或提交到 Git。模型需支持所用的文本生成接口；方法图解读还需要图片输入能力。不配置模型也可运行基础检索、规则排序和回退报告。

首次使用在“方向”页设置具体研究主题，再到“配置”页检查外部服务开关。**模板启用了邮件、自动周报和自动版本检查**；只想先体验推荐时，将下列字段合并到配置对应段：

```yaml
delivery:
  email_enabled: false
weekly:
  auto_generate: false
version_tracking:
  auto_check: false
```

随后通过界面刷新推荐，或在 Windows 使用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run.ps1 run --no-email
```

`doctor` 只做本地检查，不证明外部服务已连通。`demo` 可生成完全离线的示例日报；已有数据时请为示例设置独立输出目录。

<a id="daily-use"></a>
## 日常使用

1. **选择方向，查看推荐。** 收藏值得读的论文；“排除此论文”和“屏蔽主题词”分别控制单篇与主题过滤。
2. **按需深读。** 从推荐或文献库提交全文报告，查看方法图、结果表与原文链接。报告库区分全文分析、摘要级回退和历史质量未知。
3. **记录结论。** 更新阅读状态、已读版本、标签和笔记；通过本地搜索重新找到材料。
4. **形成阶段成果。** 生成研究周报，或选择 2–5 篇论文做固定版本比较。周报中的个人笔记默认不包含；显式选择后在模型生成完成后本地追加。
5. **最后开启自动化。** 在“方向 → 多方向调度”配置时间、时区和星期。新安装的调度默认暂停，启用后需让 GUI 或独立调度入口持续运行。

| 常用任务 | 已激活环境中的 CLI |
| --- | --- |
| 本地检查 | `paperloom doctor` |
| 推荐更新，不发邮件 | `paperloom run --no-email` |
| 指定修订版报告 | `paperloom report 2506.15903v1` |
| 查看调度状态 | `paperloom schedule --status` |
| 执行一次到期检查 | `paperloom schedule --once` |
| 查看全部命令 | `paperloom --help` |

Windows 无需激活，可用 `powershell -ExecutionPolicy Bypass -File scripts\run.ps1 <命令>` 代替 `paperloom <命令>`。项目原名 arXiv Research Assistant；旧命令 `arxiv-ra`、模块 `arxiv_ra` 和发行包名 `arxiv-research-assistant` 保持兼容。

自动调度运行推荐日报流程，**不会为每篇推荐自动生成深度报告**。错过时段后有两小时补触发窗口；失败、中断或取消的任务不自动重试。需要时从任务页显式恢复，恢复推荐任务不发邮件。报告恢复可复用兼容检查点，但不承诺每个外部调用只执行一次。完整条件见 [任务恢复](docs/USAGE.md#任务历史与中断恢复) 与 [定时运行](docs/USAGE.md#automation)。

<a id="upgrade"></a>
## 从旧版升级

1. 等待当前任务完成并正常退出 GUI / 调度进程。
2. 备份 `.env`、`config.yaml`、`profiles/` 和配置的输出目录（默认 `run/`）。应用备份默认不包含密钥，凭据请另行妥善保存。
3. 更新源码。Git 安装且工作区干净时可执行 `git pull --ff-only`；源码包安装将新版解压到独立目录后迁移上述数据。已有本地代码改动请先保存。
4. 重新运行 `setup_environment.ps1`（原来使用 Docling 的安装继续加 `-WithDocling`），或在已激活环境执行 `python -m pip install -e '.[gui]'`。
5. 执行 `doctor`，重启 GUI，并刷新浏览器。通过“任务”“调度”“配置”页检查状态，再恢复日常使用。

旧数据按兼容路径读取，历史报告会保留；旧程序不要继续写入同一输出目录。旧的独立 HTML 文件不会自动重写，通过 GUI 打开支持的旧报告时使用共用渲染流程。备份恢复到独立目录后，自动调度始终暂停，需要明确重新启用。

<a id="configuration"></a>
## 配置、数据与隐私

| 位置 | 内容 |
| --- | --- |
| `config.yaml` | 模型、输出目录、PDF、邮件、集成等全局设置 |
| `.env` | API 密钥、服务地址、SMTP 凭据及可选代理配置 |
| `profiles/` | 方向档案及活动方向 |
| `run/`（或自定义输出目录） | 推荐、报告、阅读状态、搜索缓存、任务与调度记录 |
| `.venv/` | 本地 Python 环境及依赖 |

上述个人目录均不进入源码发布包。`.gitignore` 只影响 Git 跟踪，不会在磁盘上隐藏或删除 `.venv`。

活动方向的检索和排序设置会覆盖全局同名段；任务使用提交时的配置。详情见 [配置指南](docs/USAGE.md#configuration)、[配置模板](config.example.yaml) 与 [环境变量模板](.env.example)。

启用远程模型后，论文材料、研究兴趣及方法图可能发送给所选服务。个人阅读笔记不发送给模型；选择追加到周报后，配置的 Obsidian 同步可包含这些内容。Web 界面面向本机使用，支持 `127.0.0.1` / `localhost`，校验 Host 与修改请求来源，不提供多用户登录或自定义域名反向代理部署。

<a id="faq"></a>
## 常见问题

**终端能访问 arXiv，项目却失败？** 请核对实际使用的 Python、代理环境变量、证书和请求地址。统一启动脚本会读取项目 `.env`，已有进程环境变量优先；更改后重启进程。项目包含 SOCKS 代理支持及 arXiv TLS 兼容修复，但外部服务仍可能限流或不可达。不要通过关闭证书校验排错。

**报告为何缺少图片或只有摘要？** 查看运行提示和报告质量标识。图片依赖可用的 PDF / HTML 提取结果；全文下载、解析或模型失败时可能生成回退报告。不是每篇论文都有可靠的可提取方法图。

**看到“—”或待核对提示意味着什么？** 表示当前检查无法支持该数值或发现归属冲突，不代表零，也不代表论文没有报告。详情保留原始内容；没有提示也不代表结论正确。

**重启后任务会重新跑吗？** 不会自动重放。中断任务保留历史与恢复入口；显式恢复会创建新尝试，是否复用步骤取决于检查点兼容性。

**修改了配置却未生效？** 检索与排序可能被方向设置覆盖；排队任务保留提交时快照。更改输出目录或 Python 代码后需要重启 GUI。

## 文档与开发

- [完整使用指南](docs/USAGE.md)：协作检索、恢复、搜索、备份、周报、比较、集成与调度。
- [架构说明](docs/ARCHITECTURE.md) / [领域用语](CONTEXT.md)：模块职责与存储边界。
- [全局审核](docs/GLOBAL_READINESS_REVIEW_2026-09-27.md) / [浏览器验收](docs/qa/ui/2026-09-27-final-visual.md)：本轮检查范围和限制。
- [发布检查单](docs/RELEASE_CHECKLIST.md)：构建、安装验证与数据排除规则。

```bash
python -m pip install -e '.[gui,dev]'
python -m pytest -q
python -m compileall -q src tests
python -m pip check
```

2026-09-27 发布前回归：**556 项测试通过**，保留一项既有 Starlette/httpx 弃用提示。内置 Chromium 完成 19 个页面 × 4 种宽度（1440 / 768 / 390 / 320）的验收，最终记录无整页横向溢出、图片加载失败或数学渲染错误；不等同于手机真机或全部浏览器认证。

提交问题时请附 Python / 操作系统版本、复现步骤和脱敏日志；不要上传真实密钥或完整个人数据目录。

## 致谢与许可证

感谢 arXiv、alphaXiv、OpenAlex、Semantic Scholar 及项目所依赖的开源工具。Obsidian 知识库的组织思路参考了 [dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)。本项目是独立工具，不代表上述服务的官方产品。

代码采用 [MIT License](LICENSE)。第三方依赖、字体与静态资源保留各自许可证；论文与图片的权利不因项目代码许可证而改变。
