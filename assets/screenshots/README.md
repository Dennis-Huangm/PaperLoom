# README 截图清单

主 README 的每项功能都对应一个图片位置。现有 JPG 是临时演示截图；名称带 `-placeholder.svg` 的图片是明确标注的占位卡片，不是实际界面截图。

## 替换方式

1. 在实际使用界面截图，尽量保留关键入口、输入项和操作结果。
2. 把图片保存到本目录，使用下表的文件名。已有 JPG 可直接覆盖；SVG 占位建议换成同名 JPG 或 PNG，例如 `settings-placeholder.svg` 换成 `settings.png`。
3. 如果文件名或格式变化，修改根目录 README 中对应的图片路径，并更新本清单。无需改动旁边的操作文字。

建议使用清晰的横向截图，统一浏览器缩放比例，避免把窗口周围无关区域截入。提交前确认图片不含真实凭据及不想公开的个人资料。

| 功能 | 当前图片 | 建议展示的内容 |
| --- | --- | --- |
| 配置模型 | `settings-placeholder.svg` | API 与邮箱凭据、模型名称、保存入口 |
| 建立研究方向 | `profiles-placeholder.svg` | 主题与参考论文、草稿条件、试搜与启用 |
| 每日推荐 | `recommendations.jpg` | 全部 / 高相关 / 已核实筛选、论文列表与推荐依据 |
| 会议检索 | `conferences.jpg` | 会议选择、年份、研究主题与检索入口 |
| 文献库 | `library.jpg` | 收藏列表、展开的阅读记录、标签与笔记 |
| 阅读报告 | `report.jpg` | 中文解读、方法图或表格、原文定位 |
| 跨论文比较 | `comparison.jpg` | 多篇论文版本选择、关注问题或比较结果 |
| 相关工作地图 | `graph-placeholder.svg` | 地图、关系详情、关联依据与来源状态 |
| Zotero / Obsidian 收录 | `collection-placeholder.svg` | 目标工具、保存位置、材料与完成回执 |
| 本地搜索 | `search-placeholder.svg` | 查询词、匹配片段、来源链接与索引状态 |
| 版本追踪 | `versions-placeholder.svg` | 待同步清单、修订版、预览与执行结果 |
| 研究周报 | `weekly-placeholder.svg` | 活动概览、生成入口与周报归档 |
| 多方向调度 | `schedules-placeholder.svg` | 时间、时区、总开关与执行历史 |
| 后台任务 | `tasks-placeholder.svg` | 任务进度、结果链接与恢复入口 |
| 备份与恢复 | `backups-placeholder.svg` | 备份选项、下载入口与恢复范围预览 |

## 当前演示截图来源

以下 JPG 为实际浏览器界面截图，未经合成或内容修改；仅转换为 JPEG 用于文档展示。

- `recommendations.jpg`：2026-10-01，v1.5.0 公开论文元数据在隔离演示库中的展示；分数、收藏及明确标注的摘要与依据为演示内容。
- `conferences.jpg`：2026-10-01，v1.5.0 会议检索表单与多选状态，未提交网络检索。
- `library.jpg`：v1.4.0 示例收藏、阅读状态、标签和笔记；不是个人文献库。
- `comparison.jpg`：v1.4.0 选择两篇指定修订版论文的操作界面，未提交模型生成。
- `report.jpg`：v1.4.0 RULER 报告阅读界面，包含原论文方法图与生成解读。

论文来源包括 [RULER](https://arxiv.org/abs/2609.25270v1)、[SVGenius](https://arxiv.org/abs/2506.03139v1) 与 [VectorGym](https://arxiv.org/abs/2603.29852v2)。论文文字和图示的权利属于原作者，不因 PaperLoom 的 MIT 代码许可证而改变。截图仅说明软件使用方式，模型摘要与解读不作为论文结论准确性的保证。
