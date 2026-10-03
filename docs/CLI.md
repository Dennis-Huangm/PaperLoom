# PaperLoom 命令行与自动化

日常网页操作见 [README 的功能 Quickstart](../README.md#web-workflow)，详细规则见 [功能与配置说明](USAGE.md)。本文用于安装参数、脚本运行及系统调度。命令从项目目录运行，个人配置保存在 `config.yaml` / `.env`。

<a id="installation"></a>
## 安装与启动参数

Windows 使用项目 `.venv`，无需手动激活。PowerShell 示例：

```powershell
# 默认安装，创建缺失的配置模板；已有配置不覆盖
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_environment.ps1

# 指定基础 Python；Docling 是可选全文解析器
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_environment.ps1 -Python "D:\Python313\python.exe" -WithDocling

# 改端口；-NoBrowser 只启动服务，不自动打开浏览器
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_gui.ps1 -Port 8001 -NoBrowser

# 通用命令入口
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1 doctor
```

双击入口 `Setup-PaperLoom.cmd`、`Start-PaperLoom.cmd` 调用相同的 PowerShell 脚本。安装可加 `-Dev` 安装测试依赖，或用 `-Constraints` 指定依赖约束文件；Python 3.13 默认采用仓库中的 Windows 约束。

### Windows 登录后无终端启动

个人电脑上持续开发时，可让网页以当前普通用户身份在登录后启动。以下命令从项目目录运行：

```powershell
# 注册当前用户的登录任务；不需要保存密码，不使用最高权限
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_gui_logon_task.ps1

# 当前会话立即启动，以及后续停止、重启和查看状态
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manage_gui.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manage_gui.ps1 -Action stop
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manage_gui.ps1 -Action restart
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manage_gui.ps1 -Action status
```

计划任务名称为 `PaperLoom GUI (User)`，使用基础解释器的 `pythonw.exe` 无控制台启动器，再以隐藏窗口标志启动项目 `.venv\Scripts\pythonw.exe`，避免部分 Python 版本的虚拟环境启动器闪出黑框。实际网页始终使用项目虚拟环境，登录时不打开终端或浏览器。任务在电池供电时也可运行，没有默认的运行时长限制；异常退出最多重试三次。它在用户退出登录后停止，适合登录后使用网页的个人环境。

日志保存在配置目录下的 `run\.desktop-gui\gui-8000.log`（自定义端口时文件名随端口变化），启动器错误保存在项目 `run\.desktop-gui\launcher-error.log`。重复启动不会创建第二个实例；其他程序占用端口时报告启动失败。停止和重启通过实例标识请求正常退出，不根据端口强杀进程。停止前应先等待论文任务完成。

如果此前把本项目注册为名为 `paperloom` 的 NSSM 系统服务，需要一次管理员操作来停用旧服务：在**管理员 PowerShell** 中进入项目目录，然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\switch_gui_to_logon.ps1
```

该脚本先注册普通用户登录任务，再停用并停止本目录对应的旧 NSSM 服务，最后启动新任务。它保留 NSSM 注册以及原启动方式记录，不删除服务。若管理员终端使用另一个账户，应传入 `-UserId "计算机名\日常用户名"`，确保新任务归属于日常使用账户。旧服务未停用前不要同时启动新任务，以免争用同一端口。需要回退时先停止并禁用新任务，再以管理员权限恢复 `paperloom` 服务原启动方式并启动服务。

Linux / macOS 创建并激活虚拟环境后使用标准入口：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[gui]'
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
paperloom --config config.yaml doctor
paperloom --config config.yaml gui
```

wheel 安装适用于已有 Python 环境：

```bash
python -m pip install './arxiv_research_assistant-1.5.0-py3-none-any.whl[gui]'
```

wheel 不包含用户配置和启动脚本，需要另外下载源码包或从仓库取得 [配置模板](../config.example.yaml) 与 [环境变量模板](../.env.example)，保存为 `config.yaml` / `.env` 后启动。

下面的 `paperloom` 命令需在已激活环境中执行。Windows 也可使用 `scripts/run.ps1 <命令>`。旧命令 `arxiv-ra`、模块 `arxiv_ra` 和发行包名 `arxiv-research-assistant` 保持兼容。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 查看版本与帮助 | `paperloom --version`、`paperloom --help` |
| 检查本地配置与依赖 | `paperloom --config config.yaml doctor` |
| 启动网页 | `paperloom gui` 或 `paperloom gui --port 8001 --no-browser` |
| 生成每日推荐，不发邮件 | `paperloom run --no-email` |
| 按配置生成推荐并投递 | `paperloom run` |
| 忽略已处理历史重新筛选 | `paperloom run --force --no-email` |
| 生成单篇报告 | `paperloom report 2506.15903v1` |
| 生成研究活动周报 | `paperloom weekly` |
| 周报附入个人笔记 | `paperloom weekly --include-notes` |
| 比较指定版本的本地论文 | `paperloom compare 2407.05600v1 2407.05601v2 --question "实验条件与复现成本"` |
| 检查已追踪论文的新版本 | `paperloom versions` |
| 生成相关工作地图 | `paperloom citation 2506.15903v1` |
| 同步 Obsidian | `paperloom obsidian-sync` |
| 列出和切换方向 | `paperloom profiles`、`paperloom activate your-profile-id` |
| 离线生成演示日报 | `paperloom demo` |
| 测试邮件投递 | `paperloom test-email` |

全局 `--config` 参数放在子命令之前。命令加载配置文件同目录下的 `.env`；已有进程环境变量优先。`doctor` 不联网，通过检查不代表外部 API 或邮件已连通。`test-email` 会实际发邮件，请先确认收件配置。

`report` 不带版本号时读取当前版本，带 `vN` 时固定指定修订版；`compare` 需要 2–5 篇已存在本地资料、带版本号的不同论文。每日 `run` 不会为每篇推荐自动生成全文报告。

全文报告的原文表格放在对应正文讨论中，每个编号呈现一次；较长的补充矩阵可以就地展开。生成目录同时保留冻结的表格数据 `tables.json`、正文合成草稿 `report-draft.md` 和内容保全结果 `table-preservation.json`。自动原文定位的限制见 `evidence.json`，不会因核对能力不足删除已有实验数据或把正常完成的任务显示为组件异常。

## 版本同步

```bash
paperloom sync 2407.05600
paperloom sync 2407.05600 --report --zotero --obsidian
paperloom sync 2407.05600 --target-version 3 --retry
```

默认先查询最新版本并固定本次目标。`--target-version` 显式指定目标，`--retry` 继续该版本上次选择但未完成的步骤。旧版材料保留；外部同步和报告生成遵循配置，可能访问外部服务及模型。

## 备份与独立目录恢复

```bash
paperloom backup create --reports --pdfs
paperloom backup preview "backups/your-backup.zip" --name research-copy --mode keep
paperloom backup restore "backups/your-backup.zip" --name research-copy --mode keep --token "预览返回的token"
paperloom backup reconcile
```

备份默认不含 `.env`，仅在明确需要时使用 `--secrets`，此时归档包含明文凭据。恢复写入 `restored/<name>/`，不覆盖当前应用目录；目标或归档变化后，原预览 token 失效，需要重新预览。恢复副本的自动调度始终暂停。

<a id="automation"></a>
## 多方向调度

先在 **方向 → 多方向调度** 保存各方向的时间、时区、星期与邮件选项，再启用总开关。GUI 运行时会检查计划，也可独立执行：

```bash
paperloom schedule --status
paperloom schedule --once
paperloom schedule
```

`--status` 只读，`--once` 检查一次并等待提交的任务结束；无参数常驻检查，`Ctrl+C` 停止后续提交。GUI 与独立入口共用队列占用锁，已有进程占用时第二个入口跳过。失败、中断或取消不会自动重试。

暂停计划仅停止后续提交，已排队任务需在任务页取消。不要同时启用旧版每日 `run` 系统任务与新多方向调度，以免重复运行。详细时段规则见 [网页调度说明](USAGE.md#automation)。

### Windows 计划任务

以下命令会为当前用户创建或更新兼容任务 `arXiv Research Assistant`；任务需要电脑运行且该用户已登录。注册前会用项目 Python 做本地检查。

```powershell
# 推荐：每 5 分钟检查网页中保存的多方向计划
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -ProjectDir $PWD -MultiProfile

# 单方向兼容入口：每天按当前活动方向执行 run
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -ProjectDir $PWD -At "08:00"
```

`-MultiProfile` 不会自行开启方向计划。系统任务、GUI 与 CMD 都使用项目 `.venv`，需具有一致的 `.env`、网络与代理配置。

### GitHub Actions

仓库提供 [手动日报工作流](../.github/workflows/daily.yml)，默认不定时运行。需要时在自己的仓库通过 **Actions → PaperLoom research digest → Run workflow** 执行。

在 **Settings → Secrets and variables → Actions** 设置 `PAPERLOOM_CONFIG_YAML`，内容为完整个人配置 YAML，保留 `output_dir: run`。按功能添加 `LLM_API_KEY`、`LLM_BASE_URL`、`ALPHAXIV_API_KEY`、`OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY`、`QQ_EMAIL` 和 `SMTP_PASSWORD`。云端不会读取本机研究方向，Zotero 与 Obsidian 应关闭。

工作流缓存去重状态、推荐和元数据；公开仓库缓存不能视为私有存储，个人研究数据建议在本地或私有仓库运行。报告 artifact 默认不上传，只有手动勾选上传选项时才保存，访问权限由仓库规则决定。

## 网络与 PDF 排错

浏览器访问成功不证明 Python 的网络路径可用。先运行 `doctor`，核对实际 Python、代理、证书和环境变量。更改 `.env` 后重启服务；不要用关闭证书校验排错。

Windows 可用 `scripts/start_gui_pdf_direct.cmd` 启动 PDF 专用直连模式，或设置 `ARXIV_PDF_BACKEND=curl-direct` 后重启。它使用系统 `curl.exe` 分段下载 PDF；API 和模型保留原代理配置。默认 `httpx`，改回该值可恢复。它不能绕过代理软件的 TUN 接管，也不保证远端始终可用。

arXiv 及其他服务仍可能限流。客户端保留请求间隔、退避和冷却处理，不应通过并发重试绕过限制。
