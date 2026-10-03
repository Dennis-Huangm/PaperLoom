from bs4 import BeautifulSoup
import pytest

from arxiv_ra.render import markdown_with_math


def test_report_renders_cjk_bold_followed_by_prose():
    source = (
        '4. **全场景强化学习（RL for all Scenarios）**：结合规则奖励与偏好奖励模型。'
        '在通用对齐上，**有用性（Helpfulness）**仅评估最终 summary，'
        '规避对思考过程的干扰；**无害性（Harmlessness）**评估完整响应'
        '（包含推理过程与最终结论）以确保安全合规 [6](paper.pdf#page=11)。'
    )
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert [node.get_text() for node in tree.select('strong')] == [
        '全场景强化学习（RL for all Scenarios）',
        '有用性（Helpfulness）',
        '无害性（Harmlessness）',
    ]
    assert '**' not in tree.get_text()
    assert tree.select_one('a')['href'] == 'paper.pdf#page=11'


@pytest.mark.parametrize('source,label', [
    ('**有用性（Helpfulness）**仅评估', '有用性（Helpfulness）'),
    ('采用**（规则奖励）**训练。', '（规则奖励）'),
    ('**注意！**继续。', '注意！'),
    ('采用**「规则」**训练。', '「规则」'),
    ('采用**(rules)**训练。', '(rules)'),
    ('**有用性（Helpfulness）**のみ評価', '有用性（Helpfulness）'),
    ('**유용성(Helpfulness)**평가', '유용성(Helpfulness)'),
])
def test_cjk_punctuation_boundaries_render_bold(source, label):
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert tree.strong is not None
    assert tree.strong.get_text() == label
    assert '**' not in tree.get_text()


def test_cjk_emphasis_preserves_nested_markup_table_values_and_links():
    source = ('| 项目 | 数值 |\n|---|---|\n'
              '| **有效性（*奖励*）**提高 [原文](paper.pdf#page=11) | 72.6 |\n')
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert tree.strong.get_text() == '有效性（奖励）'
    assert tree.strong.em.get_text() == '奖励'
    assert tree.select('td')[1].get_text() == '72.6'
    assert tree.a['href'] == 'paper.pdf#page=11'


def test_emphasis_keeps_literal_code_math_escapes_and_latin_boundaries():
    source = r'''`**有用性（Helpfulness）**仅评估`

\**有用性（Helpfulness）\**仅评估

$a**(b)**c$

**Helpfulness)**evaluates and foo_bar_baz

```text
**有用性（Helpfulness）**仅评估
```
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert not tree.strong
    assert not tree.em
    assert tree.select_one('.math-inline').get_text() == 'a**(b)**c'
    assert len(tree.select('code')) == 2
    assert '**Helpfulness)**evaluates and foo_bar_baz' in tree.get_text()
