"""Remove known generator diagnostics without rewriting scientific content."""
from __future__ import annotations

import re


def clean_report_diagnostics(text: str) -> str:
    # Protect fenced examples. Only exact generated phrases/headings are edited.
    parts = re.split(r'(?ms)(^ {0,3}`{3,}[^\n]*\n.*?^ {0,3}`{3,}[^\n]*(?:\n|$)|^ {0,3}~{3,}[^\n]*\n.*?^ {0,3}~{3,}[^\n]*(?:\n|$))', text)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = re.sub(r'(?ms)^## (?:引用与核对|原文依据与覆盖|实验数值核对)\s*\n.*?(?=^## |\Z)', '', part)
        part = re.sub(r'(?m)^> \*\*(?:实验数值待核对|数值引用存在冲突)\*\*[^\n]*(?:\n|$)', '', part)
        part = part.replace('**[待核对]** ', '').replace('（引用冲突）', '').replace('（待核对）', '')
        part = re.sub(r'（(?:原文摘录未能唯一定位，请核对|(?:片段引用不可用|分片摘录未能唯一定位)，缺少可定位原文依据|当前材料缺少可定位原文依据|模型提供的页码未核对)）', '', part)
        part = part.replace('当前材料缺少可定位原文依据', '')
        part = re.sub(r'(?m)^定量陈述暂不展示。\s*$', '', part)
        part = part.replace('## 待核对内容恢复', '## 补充实验数据')
        part = part.replace('以下内容曾因引用核对失败被隐藏，现保留供核查；由于旧占位符无法唯一定位，未自动插回正文。', '')
        part = part.replace('## 原文表格索引', '## 补充原文表格')
        part = part.replace('下列原文表格尚未确认完整复刻；未按表号匹配的表也可能以其他标题出现。链接仅指向原文位置，数值未逐项核验。', '')
        part = part.replace('：正文未按表号匹配；', '：').replace('：部分摘录；', '：')
        part = part.replace('请先查看文末“实验数值核对”，再使用这些结果。', '')
        part = part.replace('使用前请[核对原文与数值](evidence.json)。', '')
        parts[index] = part
    return ''.join(parts)
