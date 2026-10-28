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


## 源码导航

`src/paperloom/` 按研究任务组织实现，先从对应领域目录进入：

```text
src/paperloom/
├── discovery/       # 论文发现、筛选与每日推荐
├── profiles/        # 研究方向、检索草稿与方向小综述
├── sources/         # 原文解析、图示与来源定位
├── reports/         # 本地报告生成、呈现与报告库读取
│   └── tables/      # 表格摘录、整理、放置与兼容读取
├── reading/         # 阅读会话、引用范围与原文查阅
├── papers/          # 论文资料、阅读状态、本地检索与备份
├── versions/        # 版本追踪与材料更新
├── related_work/    # 相关工作地图
├── integrations/    # Zotero、Obsidian 与邮件
├── runtime/
│   ├── jobs/        # 持久化任务、取消、恢复与调度
│   └── model_access/ # 模型调用、连接配置与请求预算
└── web/
    └── routes/      # 按研究任务分组的页面适配
```

根目录保留命令入口和少量通用定义；`resources.py` 定位安装包中的
`templates/` 与 `static/`，实现文件移动不改变资源地址。
跨入口任务与本地检索不导入网页目录；保存到任务回执的成果链接由
`runtime/artifacts.py` 统一处理。表格的放置规则由
`reports/tables/placement.py` 统一处理，当前发布和旧格式恢复共用实现。

`tests/` 按对应领域归组，`browser/` 与 `fixtures/` 保留原位置。
`scripts/maintenance/` 保存报告修复及维护工具；Windows 启动与安装脚本
保留原位置。Python 包和发行包统一命名为 `paperloom`；命令入口为
`paperloom` 或 `python -m paperloom`，网页入口为 `paperloom.web.create_app`。

## 版本与验证

项目通过仓库源码安装与更新。版本由 `pyproject.toml` 和 `src/paperloom/__init__.py` 共同声明，可通过 `paperloom --version` 查看；功能与行为变化记录在 `CHANGELOG.md`。

提交前运行开发环境中的验证命令，检查网页、模板、静态资源及 `paperloom` / `python -m paperloom` 入口。新增或重命名的源码文件需加入 Git 跟踪；校验公开文档的相对链接和截图来源，确保个人配置、研究数据和内部记录不进入仓库。
