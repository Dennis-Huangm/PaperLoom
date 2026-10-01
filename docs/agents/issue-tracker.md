# Issue tracker: GitHub

本项目的任务与规格发布到 Dennis-Huangm/PaperLoom 的 GitHub Issues。
使用 gh CLI，并明确指定 --repo Dennis-Huangm/PaperLoom。

## 操作约定

- 创建：gh issue create --title "..." --body-file <正文文件>
- 读取：gh issue view <编号> --comments
- 列举：gh issue list --state open --json number,title,body,labels,comments
- 评论：gh issue comment <编号> --body-file <正文文件>
- 添加标签：gh issue edit <编号> --add-label "<标签>"
- 移除标签：gh issue edit <编号> --remove-label "<标签>"
- 关闭：gh issue close <编号> --comment "..."

以上命令均附带 --repo Dennis-Huangm/PaperLoom。
多行正文保存为 UTF-8 文件，再通过 --body-file 提交。

## 发布与读取

技能要求“发布到 issue tracker”时，创建 GitHub issue。
要求读取任务时，读取 issue 正文、标签和评论。
发布前检查是否已有对应 issue，避免重复创建。
标签名称遵循 docs/agents/triage-labels.md。
需要的标签不存在时，先创建标签，再应用。

## Pull requests as a triage surface

PRs as a request surface: no.
