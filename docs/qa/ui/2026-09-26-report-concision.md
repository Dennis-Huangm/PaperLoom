# 报告展示精简与渲染修复

用户反馈：单篇报告和跨论文比较的核对附录冗长，复制的原陈述显示 `###`、`**`、反引号等语法；磁盘 HTML 中 `512 \\times 512` 未渲染。

## 原因与调整

- 本地 HTML 使用网站 `/static` 绝对路径，file URL 无法找到 KaTeX；磁盘产物现在定位到包内资源，HTTP 视图继续使用 `/static`。未放宽 HTML 清洗或 CSP。
- 核对附录将模型报告作为字面文本再次输出，Markdown 标记因此可见。移除重复正文而不将引用文本重新解释为可执行 HTML/Markdown。
- 新旧跨论文比较不再展开“条目依据与待核对原因”“固定证据摘录”。矩阵来源引用直接指向 PDF、原报告或固定材料快照，保留七个比较维度、推断/待核对标签和可比性说明。
- 单篇报告不再重复逐条原文和数值“原陈述”；保留页码链接、顶端数值警告及简短覆盖说明。完整核验数据保留在 evidence.json；比较来源及判定数据保留在 sources.json、matrix.json。
- 历史报告由 presentation 层适配；新 Markdown 直接生成精简格式。Obsidian 导出同样适配，复制存在的 evidence.json 并重写详情入口；缺少历史详情文件时显示不可用说明。
- 检查周报：活动时间线和用户选择附入的个人笔记为独立信息，未删除。没有调用真实 Obsidian 或其他外部集成。

## 验证

- 回归先复现三个失败：旧核对内容仍出现、删附录后的来源链接需求、原陈述重复。修复后覆盖新旧报告、详情数据保留、PDF 与报告来源链接及临时 vault 导出。
- 最终离线回归：**438 passed, 1 warning, 40.82s**，日志 `work/report-concision-final-tests.log`。唯一警告为既有 Starlette TestClient/httpx 弃用提醒。
- `compileall`、report.js 语法和 `git diff --check` 通过。
- 通过生产 `ReportStaticFiles` 在隔离的 localhost:8771 验证比较矩阵及精简目录；VectorEdits 的 7 处公式已由 KaTeX 渲染，错误节点为 0，截图确认 `512 × 512` 显示正常。
- 所有 9 份重渲染产物的正文来源和详情链接经本地文件存在性检查。file URL 受浏览器工具限制，未直接在浏览器控制中打开；本地资产路径由自动化测试验证。完整桌面/窄屏验收仍延后。
- 临时浏览器页及本轮 QA 服务已关闭。未调用模型、SMTP、真实 Zotero 或个人 vault。

## 已有产物与边界

更新以下 work 数据集下共 9 份 HTML：source-spans-live-20260925、quality-regeneration-20260925、long-spans-live-20260925。原 HTML、来源 Markdown 哈希及重渲染前后哈希保存在 `work/report-presentation-backups-20260926/manifest.json` 及同目录备份中。上轮资产路径修复前的备份另存于 `work/report-presentation-backups-20260925/`。

当前跨论文比较 article 文本从 33,421 字符缩短到约 2,400 字符；VectorEdits 从 17,144 字符缩短到 5,922 字符。统计包含标点和英文字符，不是汉字数，也不代表模型质量评分。Markdown、证据 JSON 和 PDF 未重写；此前质量记录中的旧 HTML 哈希可在备份中对应。

独立 HTML 仍依赖本机安装目录资源，并非可单文件搬运的完整离线包。长论文真实生成结果已保留，但其后续质量审阅仍暂停，本轮仅处理展示问题。
