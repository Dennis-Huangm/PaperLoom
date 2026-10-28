"""Place report tables and source pages beside the owning discussion."""
import re

from .table_extraction import section_headings as _headings


def find_discussion(report, title):
    """Find a topic section before falling back to a broad result section."""
    headings = _headings(report)
    if re.search(r'comparison.*(?:benchmark|dataset)|taxonomy|paradigm|范式', title, re.I):
        root, topic = '与已有工作的区别', None
    elif re.search(r'complexity|复杂度', title, re.I):
        root, topic = '核心方法', r'复杂度|数据|关键模块|流程|complexity'
    elif re.search(r'cost|token statistics|成本|费用', title, re.I):
        root, topic = '可复现性', None
    elif re.search(r'training.*(?:data|statistic)|hyperparameter|训练数据|超参数', title, re.I):
        root, topic = '实验设置', None
    else:
        root = '关键结果'
        if re.search(r'overall.*rank|rank.*evaluation|winrate|全局.*排名|总体.*排名', title, re.I):
            topic = r'排名|胜率|rank|winrate'
        elif re.search(r'understanding|理解', title, re.I):
            topic = r'理解|understanding'
        elif re.search(r'editing|编辑', title, re.I):
            topic = r'编辑|editing'
        elif re.search(r'image-to-svg|图像到\s*SVG|图像生成', title, re.I):
            topic = r'image-to-svg|图像到\s*SVG|图像生成'
        elif re.search(r'generation|生成', title, re.I):
            topic = r'生成|generation'
        else:
            return None
    parent = next((h for h in headings if h[2] == 2 and h[3] == root), None)
    if parent is None:
        return None
    stop = next((h[0] for h in headings if h[0] > parent[0] and h[2] <= 2), len(report))
    candidates = [h for h in headings if parent[0] < h[0] < stop and h[2] == 3
                  and topic and re.search(topic, h[3], re.I)
                  and not re.match(r'^(?:Table\s*\d+|表(?:格[：:]\s*)?\s*\d+)', h[3], re.I)]
    if topic == r'生成|generation':
        candidates = [h for h in candidates if not re.search(r'排名|rank|winrate', h[3], re.I)]
    if topic == r'编辑|editing':
        candidates = [h for h in candidates if not re.search(r'失败机制|失败模式|成功与失败|定性', h[3])]
    if not candidates and root == '关键结果':
        candidates = [h for h in headings if parent[0] < h[0] < stop and h[2] == 3
                      and re.search(r'主评测|主实验|主(?:要)?结果', h[3])]
    return candidates[0] if candidates else parent

def destination(report, number, title):
    headings = _headings(report)
    reference = re.compile(rf'\bTable\s+{number}\b|表\s*{number}(?!\d)', re.I)
    matches = [h for h in headings if reference.search(h[3])]
    # Existing table sections anchor their own footnotes. A missing table uses
    # the experiment topic, not a broad paragraph listing several table IDs.
    table_heading = [h for h in matches if re.match(r'^(?:Table\s*\d+|表(?:格[：:]\s*)?\s*(?:Table\s*)?\d+)', h[3], re.I)]
    discussion = find_discussion(report, title)
    if table_heading:
        matches = table_heading
    elif discussion:
        matches = [discussion]
    if not matches:
        # A paragraph reference also anchors an omitted table to its discussion.
        for i, heading in enumerate(headings):
            end = headings[i + 1][0] if i + 1 < len(headings) else len(report)
            if reference.search(report[heading[1]:end]):
                matches.append(heading)
    if not matches:
        if re.search(r'cost|token statistics|成本|费用', title, re.I):
            target = '可复现性'
        elif re.search(r'paradigm|taxonomy|范式', title, re.I):
            target = '与已有工作的区别'
        elif re.search(r'training.*(?:data|statistic)|hyperparameter|训练数据|超参数', title, re.I):
            target = '实验设置'
        else:
            target = '关键结果'
        matches = [h for h in headings if h[3] == target]
        if not matches:
            report = report.rstrip() + f'\n\n## {target}\n\n'
            return report, len(report), 3
    heading = matches[0]
    # Source tables are siblings even if a legacy restoration nested their
    # Markdown headings. A table's notes must stop at the next numbered table,
    # not at the end of the enclosing discussion.
    def numbered_table(h):
        return re.match(r'^(?:Table\s*\d+|表\s*\d+)[：:.\s]', h[3], re.I)

    end = next((h[0] for h in headings if h[0] > heading[0]
                and (h[2] <= heading[2] or (table_heading and numbered_table(h)))), len(report))
    level = heading[2] if table_heading else max(4, min(heading[2] + 1, 6))
    return report, end, level
