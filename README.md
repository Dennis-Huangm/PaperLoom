<p align="center">
  <img src="assets/arxiv-research-assistant.png" width="104" alt="PaperLoom 标志">
</p>

<h1 align="center">PaperLoom · 知织</h1>

<p align="center">
  <strong>把论文织成知识。</strong><br>
  Weave papers into understanding.<br>
  在本地网页中发现论文、阅读全文、管理研究材料。
</p>

<p align="center">
  <a href="https://github.com/Dennis-Huangm/PaperLoom/releases/latest"><img alt="最新版本" src="https://img.shields.io/github/v/release/Dennis-Huangm/PaperLoom"></a>
  <img alt="Python 3.11 或更新版本" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-green"></a>
</p>

<p align="center">
  <a href="#quick-start">安装与启动</a> ·
  <a href="#web-workflow">功能 Quickstart</a> ·
  <a href="#upgrade">升级</a> ·
  <a href="docs/USAGE.md">功能与配置说明</a> ·
  <a href="docs/CLI.md">命令行与自动化</a>
</p>

PaperLoom 是面向个人研究者的论文助手：根据研究方向发现 arXiv 和会议论文，生成中文阅读报告，记录笔记，再将材料收录到 Zotero 或 Obsidian。推荐、文献库、相关工作地图、论文比较、版本跟进和周报都可以在网页中完成。

**当前正式版本：v1.6.0。** [发布说明](docs/RELEASE_NOTES.md) · [变更记录](CHANGELOG.md)。应用和研究数据保存在本机；联网检索与模型生成使用你配置的服务。

v1.6.0 改进了报告阅读与后台运行：实验表格按原文数据保全并放在对应讨论处，长表可以展开，打印时包含完整矩阵；报告目录和本地论文侧栏支持收起、搜索和切换。Windows 可在登录后无终端启动，任务页可直接调整并行任务数。

<a id="quick-start"></a>
## 安装与启动

需要 **Python 3.11+** 和可用的网络。从 [最新 Release](https://github.com/Dennis-Huangm/PaperLoom/releases/latest) 下载 **`paperloom-1.6.0-source.zip`**，解压到固定目录。发布包需要安装 Python，首次安装会下载依赖。

### Windows

1. 安装 Python，并在安装时勾选加入 PATH。
2. 双击 **`Setup-PaperLoom.cmd`**，等待环境与依赖安装完成。
3. 双击 **`Start-PaperLoom.cmd`**，浏览器会打开 [本地网页](http://127.0.0.1:8000)。

以后只需运行 `Start-PaperLoom.cmd`。使用期间保持服务窗口开启；在窗口中按 `Ctrl+C` 可退出。

如需登录后自动在后台运行，先完成安装并停止已打开的服务窗口，再从项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_gui_logon_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manage_gui.ps1 -Action start
```

之后打开 [本地网页](http://127.0.0.1:8000) 即可使用，无需保持终端窗口。任务使用当前普通用户身份，退出 Windows 登录后停止。停止、重启、日志和旧 NSSM 服务迁移见 [Windows 后台启动说明](docs/CLI.md)。

### Linux / macOS

在解压目录打开终端，安装并启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[gui]'
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
paperloom gui
```

以后激活同一环境，运行 `paperloom gui`。指定 Python 路径、更改端口等操作见 [安装与启动参数](docs/CLI.md#installation)。

<a id="web-workflow"></a>
## 功能 Quickstart

**首次使用建议：配置模型 → 建立研究方向 → 刷新推荐 → 收藏论文 → 生成阅读报告。** 其余功能按需要使用。

下面每项功能都留有截图位置：现有界面图作为临时示例，“截图待替换”图用于补齐其余位置。替换文件和推荐截图范围见 [截图清单](assets/screenshots/README.md)。

### 1. 配置模型

打开 **配置 → API 与邮箱凭据**，填写模型 API Key、Base URL，点击 **保存 API 与凭据**。再到 **模型调用** 填写服务支持的模型名称，点击 **保存全部运行配置**。需要解读方法图时，选择支持图片输入的模型。

![截图占位：模型凭据与模型调用设置](assets/screenshots/settings-placeholder.svg)

### 2. 建立研究方向

打开 **方向**，填写主题、重点关键词或参考论文 arXiv ID，点击 **生成草稿**。检查并修改检索条件，**保存修改 → 启用此方向**；想先看效果，可点击 **开始试搜**。已有方向用 **切换到此方向** 切换。

![截图占位：创建方向与启用草稿](assets/screenshots/profiles-placeholder.svg)

### 3. 获取每日推荐

打开 **任务 → 刷新每日推荐**，点击 **执行检索**；完成后到 **文献推荐** 点击论文，查看推荐依据和摘要。用 **全部 / 高相关 / 已核实** 筛选，点击 **加入文献库** 收藏；历史推荐在 **每日记录** 中查看。

**加入文献库** 会作为正向反馈帮助学习阅读偏好。需要过滤同类论文时，可 **屏蔽主题词**；误操作在 **反馈与筛选记录** 撤销。

![临时示例截图：每日推荐、筛选与论文详情](assets/screenshots/recommendations.jpg)

### 4. 检索会议论文

打开 **会议论文**，勾选会议、设置起止会议年份，选择研究方向或填写临时主题，点击 **检索会议论文**。查看检索进度与来源状态，在结果中收藏需要阅读的论文；后续可从文献库生成报告。

![临时示例截图：会议范围、年份和主题设置](assets/screenshots/conferences.jpg)

### 5. 管理文献与阅读记录

打开 **文献库**，按标题、作者、标签或笔记查找收藏。展开论文的 **阅读记录**，填写阅读进度、已读版本、标签和个人笔记，点击 **保存阅读记录**；需要阅读全文时点击 **生成论文报告**。

![临时示例截图：文献库与阅读记录](assets/screenshots/library.jpg)

### 6. 生成与阅读报告

打开 **任务 → 生成完整阅读报告**，输入 arXiv ID，点击 **开始生成**。可用 `2506.15903v1` 这样的 ID 指定修订版。完成后点击任务的结果链接，或到 **报告** 打开中文解读、方法图、公式和实验表格，并通过原文定位核对结论。

阅读时用 **目录** 跳转小节，用 **论文** 侧栏搜索和切换本地报告；两侧均可收起以扩大正文空间。原文实验表格在相关讨论处呈现，每个编号只出现一次，长表就地展开，打印时自动展开全部矩阵。报告保留已提取的数值、条件和表注；引用定位失败不会删除这些内容，明确冲突会局部标注。完整性与数值核对结果仍需结合原论文判断。

![临时示例截图：中文报告与原文定位](assets/screenshots/report.jpg)

### 7. 比较多篇论文

从 **文献库 / 报告 → 跨论文比较** 进入，勾选 **2–5 篇不同论文的指定版本**，填写关注的问题，点击 **生成比较矩阵**。完成后打开比较结果，对照研究问题、方法、实验条件和适用边界。

![临时示例截图：选择论文与生成比较矩阵](assets/screenshots/comparison.jpg)

### 8. 探索相关工作

打开 **图谱**，输入起点论文的 arXiv ID，点击 **生成地图**。完成后打开地图，查看引用关系、内容相似论文及关联依据，从候选中继续寻找值得读的工作。

![截图占位：相关工作地图与关系详情](assets/screenshots/graph-placeholder.svg)

### 9. 收录到 Zotero / Obsidian

先在 **配置** 中启用 Zotero 或 Obsidian，设置 Zotero 连接或本机 vault 路径并保存。再从推荐、文献库或报告点击 **收录与同步**，确认材料与保存位置，勾选工具后点击 **收录所选目标**；在各目标的回执中检查结果并打开条目或笔记。

![截图占位：收录目标、保存位置与回执](assets/screenshots/collection-placeholder.svg)

### 10. 搜索本地资料

打开 **搜索**，首次使用先点击 **更新索引**，完成后刷新页面。输入关键词、论文标题或 arXiv ID，点击 **搜索资料**，从结果跳转到报告、笔记或本地 PDF 的对应内容。新增报告或修改笔记后，再更新一次索引。

![截图占位：本地搜索结果与索引入口](assets/screenshots/search-placeholder.svg)

### 11. 跟进论文新版本

打开 **追踪**，选择 **全部 / 本地报告 / Zotero** 查看范围，再检查新版本。仅追踪已有报告或已收录到 Zotero 的论文；两边材料分别显示版本状态。在“待更新”中勾选并预览，确认后只更新已有且落后的材料，旧版资料继续保留。版本不明的附件进入“待核实”，页面手动检查不会自动执行更新。

![截图占位：待同步清单与版本预览](assets/screenshots/versions-placeholder.svg)

### 12. 生成研究周报

打开 **周报**，按需要勾选附入个人笔记，点击 **生成研究活动周报**。完成后从本页归档打开，回顾近期推荐、收藏、阅读进展和版本更新；个人笔记在生成后本地附入，不发送给模型。

![截图占位：研究活动概览与周报归档](assets/screenshots/weekly-placeholder.svg)

### 13. 设置定时推荐

打开 **方向 → 多方向调度**，为各方向设置时间、时区和星期，点击 **保存此方向计划**，再 **启用自动调度**。保持电脑和应用运行，到本页查看执行历史；需要邮件时先配置邮箱，再勾选 **发送推荐邮件**。

![截图占位：方向计划、总开关与执行历史](assets/screenshots/schedules-placeholder.svg)

### 14. 查看任务与运行状态

打开 **任务** 查看后台进度、日志和结果，离开页面后任务会继续运行。报告回退或中断时，检查提示后使用可用的 **继续全文分析 / 重新执行**。遇到连接或环境问题，到 **状态** 查看诊断信息。

在队列旁的 **并行任务** 选择 **1–8 个**，设置立即保存并生效。调低上限时，正在运行的任务会继续完成，后续任务按新上限启动。

![截图占位：后台队列、结果链接与恢复操作](assets/screenshots/tasks-placeholder.svg)

### 15. 备份与恢复

打开 **配置 → 备份与恢复**，选择是否包含报告和 PDF，点击 **创建并校验备份**，下载 ZIP 保存。恢复时填写备份路径与目标目录，点击 **校验并预览**，核对范围和冲突后确认恢复。默认备份不包含 `.env` 密钥。

![截图占位：备份选项与恢复预览](assets/screenshots/backups-placeholder.svg)

需要了解筛选规则、来源覆盖、配置字段或同步边界时，阅读 [功能与配置说明](docs/USAGE.md)；终端命令和系统自动化见 [CLI 文档](docs/CLI.md)。

<a id="upgrade"></a>
## 从旧版升级

1. 等待任务结束，停止旧版服务与调度，备份 **`.env`、`config.yaml`、`profiles/` 和输出目录**（默认 `run/`）。
2. 将新版解压到独立目录，复制上述数据，重新运行安装与启动入口。安装脚本会保留已有配置。
3. 检查方向、文献库和历史报告，再恢复调度；新旧程序不要同时写入同一输出目录。

使用 Windows 登录任务的用户，应先从旧目录停止后台服务，再从新目录重新注册登录任务并启动，确保计划任务指向新版安装。

## 数据与问题反馈

配置与凭据保存在 `config.yaml` / `.env`，研究方向在 `profiles/`，推荐、报告、笔记和任务记录在 `run/` 或自定义输出目录。这些个人数据不进入 Git 仓库与发布包。

网页默认在 `127.0.0.1:8000` 供本机单用户使用。网页打不开时先检查服务窗口；检索或模型任务失败时先看 **任务 / 状态** 的提示。更多运行规则见 [功能与配置说明](docs/USAGE.md)。

[问题反馈](https://github.com/Dennis-Huangm/PaperLoom/issues) 请附版本、复现步骤和脱敏错误信息。参与开发见 [贡献与验证](CONTRIBUTING.md)。

## 致谢与许可证

感谢 arXiv、alphaXiv、OpenAlex、Semantic Scholar、OpenReview 及项目依赖的开源工具。Obsidian 知识库组织思路参考了 [dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)。本项目是独立工具。

代码采用 [MIT License](LICENSE)。第三方依赖、字体、论文文字与图示保留各自许可证和权利，软件截图仅用于展示操作方式。
