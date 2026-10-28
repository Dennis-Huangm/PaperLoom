from bs4 import BeautifulSoup
import pytest

from paperloom.reading.reading_chat import ReadingService


@pytest.mark.parametrize('literal', ['`<path>`', '<path>'])
def test_reading_preserves_svg_tag_names(tmp_path, literal):
    service = ReadingService(tmp_path, None)
    sid = 'a' * 32
    text = f'原生 SVG 的 {literal} 较少（平均约 4 个）。'
    service.save({'id': sid, 'references': [], 'messages': [{
        'role': 'assistant', 'status': 'completed', 'text': text,
        'citations': [],
    }]})
    message = service.present(sid)['messages'][0]
    tree = BeautifulSoup(message['html'], 'html.parser')
    assert '<path>' in tree.get_text()
    assert not tree.find('path')
    assert message['text'] == text
    if literal.startswith('`'):
        assert tree.code.get_text() == '<path>'


def test_reading_literal_html_keeps_code_math_and_verified_citations(tmp_path):
    service = ReadingService(tmp_path, None)
    sid = 'b' * 32
    source = service.folder / 'sources' / 'paper.pdf'
    source.parent.mkdir()
    source.touch()
    text = ('```xml\n<path d="M 0 0"/>\n```\n\n'
            '比较 $a < b > c$。\n\n'
            '<script>alert(1)</script><img src="https://example.com/tracker">\n\n'
            '<a href="https://example.com">伪造引用</a> '
            '<https://example.com> [伪造链接](https://example.com) '
            '[[来源:verified]]')
    service.save({'id': sid, 'references': [], 'messages': [{
        'role': 'assistant', 'status': 'completed', 'text': text,
        'citations': [{'id': 'verified', 'source_id': 'paper', 'page': 6,
                      'url': '/source/paper#page=6'}],
    }]})
    tree = BeautifulSoup(service.present(sid)['messages'][0]['html'], 'html.parser')
    assert tree.pre.code.get_text().strip() == '<path d="M 0 0"/>'
    assert tree.select_one('.math-inline').get_text() == 'a < b > c'
    assert not tree.select('script, img, path')
    assert [(a.get_text(), a['href']) for a in tree.find_all('a')] == [
        ('第6页', '/source/paper#page=6')]
