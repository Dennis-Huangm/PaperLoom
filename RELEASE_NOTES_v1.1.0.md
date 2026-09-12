# arXiv Research Assistant v1.1.0

发布日期：2026-08-22

## 新增：Obsidian 知识库联动

v1.1.0 在 v1.0.0 的推荐、报告、周报、版本追踪、图谱与 Zotero 工作流之上，增加本地 Obsidian 知识库导出。

同步内容：

- `Daily/`：按研究方向保存每日论文推荐；
- `Papers/`：论文元数据、摘要、推荐理由和完整阅读报告；
- `Weekly/`：每周研究综述与关联论文；
- `Profiles/`：当前研究方向、检索轴线和关键词；
- `Concepts/`：从当前方向关键词中匹配的概念及论文反向链接；
- `Indexes/`：论文、每日推荐、周报和概念 MOC；
- `Attachments/`：报告方法图，以及用户显式启用后的 PDF。

论文笔记顶部使用模型精炼的中文摘要：优先复用完整报告中的“一句话总结”，其次使用推荐模型理由，必要时才额外调用模型。原始 arXiv 英文摘要不再整段写入 Obsidian，精炼结果会缓存以避免重复调用。

## 用户编辑保护

每篇笔记由 YAML frontmatter、应用托管区和用户区组成。应用只替换：

```html
<!-- ARXIV_RA_MANAGED_START -->
...
<!-- ARXIV_RA_MANAGED_END -->
```

标记外的“我的笔记”“我的周总结”等内容在重复同步时保留。若目标文件已存在但不是应用管理的笔记，系统不会覆盖，而是生成带 `(arXiv RA)` 后缀的备用文件。

## 使用

在 GUI“配置 → Obsidian”中选择 vault、保存配置，然后前往“任务 → 同步 Obsidian 知识库”。也可以运行：

```powershell
arxiv-ra --config config.yaml obsidian-sync
```

同步完成后可从配置页点击“在 Obsidian 打开”。

## 设计来源与差异

目录约定、frontmatter、双向链接、概念页和 MOC 思路参考了 `huangkiki/dailypaper-skills`。本项目没有照搬其 Agent skill 编排、Zotero SQLite 访问、自动删笔记重建或 git 自动提交，而是复用现有 FastAPI GUI、研究方向配置、Zotero Local API、主动报告生成和反馈闭环。
