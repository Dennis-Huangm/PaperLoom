# 贡献与验证

欢迎通过 [GitHub Issues](https://github.com/Dennis-Huangm/PaperLoom/issues) 提交可复现的问题或功能建议。请提供版本、平台、复现步骤、预期结果和脱敏日志，不要上传 `.env`、真实凭据、个人配置或研究数据。

## 开发环境

克隆仓库，在独立 Python 环境中安装：

```bash
python -m pip install -e '.[gui,dev]'
python -m pytest -q
python -m compileall -q src tests
python -m pip check
```

Windows 也可运行 `scripts/setup_environment.ps1 -Dev`，再使用 `.venv/Scripts/python.exe`。核心回归测试使用隔离目录与模拟服务，不调用真实模型。`tests/browser/` 的可选浏览器验收需自行准备 Playwright。

源码、模板、静态资源、用户文档及测试均可参与贡献。保证研究方向与论文修订版的边界，保留历史成果，并在独立演示数据上验证页面。用户数据不应成为测试依赖。

## 构建发布

请克隆完整 Git 仓库进行构建；面向普通用户的源码 ZIP 不附带测试和发布工具。

版本由 `pyproject.toml` 和 `src/arxiv_ra/__init__.py` 共同声明。更新它们、`CHANGELOG.md`、`docs/RELEASE_NOTES.md` 和 README 的下载名后，在 Windows 项目环境中执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_release.ps1 -Version 1.6.0
```

构建读取 Git 跟踪文件并应用用户源码包的内容白名单，生成 ZIP、wheel 和 SHA-256 校验文件到 `release/v<version>/`。源码包包含用户指南、配置模板、启动入口与运行代码；测试和构建工具在 Git 仓库中维护。新增运行文件需先加入 Git 跟踪。

发布前运行完整测试及依赖检查，验证 ZIP 无个人配置、缓存或内部记录；从解压包和干净环境安装 wheel，检查网页、模板与资源，以及 `paperloom` / `arxiv-ra` 入口。校验所有用户文档相对链接和截图来源。发布 commit 与版本 tag 后上传同一构建的附件，核对下载后的 SHA-256。

已发布附件和历史 tag 保持原样；后续修正使用新版本。构建预览可用 `-OutputDirectory work/package-preview`，不要覆盖已有发布目录。
