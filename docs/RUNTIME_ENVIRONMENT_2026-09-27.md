# 正式运行环境统一

本机已建立项目独立 `.venv`，GUI、命令行统一脚本和 Windows 计划任务安装脚本均使用该环境。正式模型已按用户选择切换为 `gemini-3.8-flash-high`。本轮不安装或开启定时任务，不执行邮件或外部知识库同步。

## 原因与本机状态

`.env` 是配置/凭据文件，`.venv` 是 Python 与依赖目录；`.gitignore` 不会隐藏或删除本地目录。此前 `.venv` 确实不存在，命令行和 GUI 使用 `D:\miniconda3\python.exe`，而 Windows 定时安装脚本要求项目 `.venv`。

现在的解释器为 `D:\arxiv-research-assistant-1.2.0\.venv\Scripts\python.exe`，Python 3.13.11，`include-system-site-packages=false`。基础解释器仍由 Miniconda 提供，不能删除或搬走基础 Python 后期望此虚拟环境继续工作；需要迁移时重新创建环境。

安装了项目 GUI、Docling 和开发测试依赖，并在项目依赖中明确加入 SOCKS 支持、Windows 时区数据，以及 Windows Docling 所需的 ONNX Runtime。没有向 Miniconda 全局环境安装新依赖。

## 入口与版本约束

- `scripts/setup_environment.ps1`：创建或更新项目 `.venv`，可选 `-WithDocling`、`-Dev`、`-Python`、`-Constraints`；已有不完整目录会报错，不自动删除。安装后执行 `pip check`。
- `scripts/run.ps1`：固定解释器、项目工作目录和默认配置路径，支持显式 `-ConfigPath`，返回子进程退出码。
- `scripts/start_gui.ps1`：调用统一入口，不再回退到 PATH 中其他 Python。
- `scripts/install_windows_task.ps1`：仍使用同一个 `.venv` 和绝对配置路径；注册前执行离线 `doctor`，失败时不注册任务。
- `doctor`：显示当前 Python 和配置路径，保留代理/证书初始化检查，便于核对实际使用的环境。
- `requirements/windows-py313-constraints.txt`：Windows Python 3.13 的已验收版本约束，安装脚本默认采用；其他 Python 版本需重新验收。发布构建包含此文件夹。

版本约束没有锁定 Python、模型权重或 wheel 哈希，不是跨平台或离线安装保证。Docling/RapidOCR 首次使用仍可能需要模型文件。

## 个人配置

- `config.yaml` 只修改 `llm.model`，切换为用户确认的 `gemini-3.8-flash-high`。密钥环境变量名、服务地址引用和其他配置不变。
- `.env` 保留原始字节内容，在末尾新增当前可用的本机 HTTP_PROXY/HTTPS_PROXY 与 NO_PROXY。没有修改系统或 Clash 配置。
- CLI 从所选配置文件目录加载 `.env`，已存在的进程环境变量仍优先。计划任务不必依赖启动它的终端携带这些代理变量。
- 使用哈希核对：原 `.env` 内容原样保留，活动方向未变，配置文件除模型名外未变。
- `.env` 与 `.venv` 继续由 Git 忽略，不提交凭据或环境目录。

## 实际验收

1. 安装后 `pip check` 通过；重新执行安装脚本，自动选择版本约束并复用现有环境，再次通过检查。
2. 从项目目录外执行统一入口 `doctor`：显示项目 `.venv` 与正式配置路径，所有必要检查通过；保留原有 SOCKS 变量时也能初始化客户端。
3. 清除子进程继承的代理变量后，项目 `.env` 能补齐 HTTP/HTTPS 代理，加载正式模型和现有凭据。此检查未修改系统环境。
4. 正式配置单次模型请求成功，请求 `gemini-3.8-flash-high`，服务返回 `gemini-3.8-flash`，响应非空。没有生成新的深度报告。
5. arXiv 指定论文 `2506.15903v1` 查询成功；按当前研究方向进行的组合检索小样本查询仍返回 HTTP 406。环境统一没有消除这一外部请求波动，不能据此宣布每日检索全链路稳定。
6. 通过 `scripts/run.ps1` 实际启动隔离配置的 GUI，从项目目录外启动成功；`/generate`、`/schedules`、`/settings` 均 HTTP 200。测试服务已关闭。这是启动与 HTTP 检查，不是新增视觉验收。
7. 使用此前已下载的 5 页 VectorEdits PDF，正式 `PDFParser` 在新环境实际使用 Docling，正文 18,026 字符，19 个图片候选，无 Docling 错误文件；未修改正式报告或个人库。

PDF 验收揭示了一个原先未声明的依赖：全局环境有 ONNX Runtime，新环境最初没有。Docling 自动改选 Torch OCR 并下载另一组权重。首次尝试被明确中止；随后补充 Windows OCR 依赖、安装原有 ONNX Runtime 1.20.1，并复制校验本机既有的 3 个 ONNX 权重文件，在独立输出目录重新解析成功。首次尝试日志保留，未改写为成功。

PowerShell 回归使用临时虚拟环境和测试模块，覆盖含空格路径、项目外启动、GUI 参数传递、子进程退出码、缺失环境拒绝回退，以及失败预检阻止注册。测试没有创建真实 Windows 计划任务。

补齐 OCR 依赖后最终全量回归：**508 passed, 1 warning in 43.87s**，日志 `final-tests.log`。唯一警告为原有 Starlette TestClient/httpx 弃用提示。`compileall`、`pip check` 和 `git diff --check` 通过。

## 日常使用

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_gui.ps1
powershell -ExecutionPolicy Bypass -File scripts\run.ps1 doctor
```

命令行也可直接使用 `.\.venv\Scripts\python.exe -m arxiv_ra --config config.yaml ...`。已运行的旧 GUI 需正常退出后使用新入口启动。裸 `python` / `paperloom` 是否指向项目环境取决于当前 shell 的激活状态；统一脚本不依赖该状态。

本轮记录位于 `work/runtime-unification-20260927/`，包括安装日志、依赖约束、代理/配置核对、模型与 arXiv 连通结果、GUI 检查、PDF 解析结果和测试日志。后续应单独处理组合检索的间歇性 406，补充新推荐到可靠版本报告的验收，再按需要设置定时触发。
