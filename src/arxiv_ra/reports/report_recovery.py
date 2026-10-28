"""Recover withheld legacy report content from its saved audit, without an LLM."""
from __future__ import annotations

from copy import deepcopy
import re

from .evidence import TOKEN
from .quality import normalized_excerpt, preserve_unverified_content


def recover_numeric_content(report: str, evidence: dict) -> tuple[str, dict, dict]:
    original_audit = evidence.get('numeric_audit', {})
    stats = {'restored_in_place': 0, 'appended_for_review': 0}
    if not original_audit.get('issues') or original_audit.get('publication_policy') in {'preserve_unverified_v1', 'table_diagnostics_v2', 'report_diagnostics_v3'}:
        return report, evidence, stats
    result = deepcopy(evidence)
    audit = result['numeric_audit']
    audit['previous_publication'] = deepcopy(audit.get('publication', {}))
    previous = [issue['replacement'] for issue in audit['issues']]
    preserve_unverified_content(audit, TOKEN)
    citations = evidence.get('citations', [])
    labels = {}
    for match in re.finditer(r'\[([^\]\n]+)\]\(paper\.pdf#page=(\d+)\)', report):
        number = re.fullmatch(r'(?:原文\s*)?(\d+)(?:\s*·\s*PDF\s*第\s*\d+\s*页)?', match[1])
        if number:
            labels[int(number[1])] = match[0]

    def resolve(match):
        citation = next((c for c in citations if (
            c.get('source_id') == match[2].strip() if match[2] is not None else
            normalized_excerpt(c['quote']) == normalized_excerpt(match[1]))), None)
        if citation is None:
            return '（原文摘录未能唯一定位，请核对）'
        return labels.get(citation['id'], f'[{citation["id"]}](paper.pdf#page={citation["page"]})')

    additions = []
    for before, issue in zip(previous, audit['issues']):
        if not (issue['action'] == 'flag_claim' or issue.get('flagged_cells')):
            continue
        before, after = TOKEN.sub(resolve, before), TOKEN.sub(resolve, issue['replacement'])
        if report.count(before) == 1:
            report = report.replace(before, after, 1)
            stats['restored_in_place'] += 1
        else:
            # Repeated/coalesced placeholders cannot safely identify a location.
            check = next((c for c in audit.get('table_checks', []) if c['start'] == issue['start']), None)
            if check:
                headers = check['headers']
                after = '| ' + ' | '.join(headers) + ' |\n|' + '---|' * len(headers) + '\n' + after
            additions.append(after)
            stats['appended_for_review'] += 1
    if additions:
        report += ('\n\n## 待核对内容恢复\n\n以下内容曾因引用核对失败被隐藏，现保留供核查；'
                   '由于旧占位符无法唯一定位，未自动插回正文。\n\n' + '\n\n'.join(additions) + '\n')
    report = re.sub(r'(?m)^> \*\*实验数值待核对\*\*[^\n]*',
                    '> **实验数值待核对**：未确认内容已保留并标注，请勿作为已核实结论；明确归属冲突的单元格以“—”显示。[查看核对详情](evidence.json)。', report)
    audit['content_recovery'] = stats
    return report, result, stats
