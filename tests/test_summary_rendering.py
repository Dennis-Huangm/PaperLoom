from bs4 import BeautifulSoup

from arxiv_ra.render import summary_html


def test_summary_uses_shared_math_markdown_and_preserves_literal_code():
    doc = BeautifulSoup(summary_html(r"**结果**：\(94.0/100\)，$x_i$；代码 `\(raw\)`。"), "html.parser")
    assert doc.strong.get_text() == "结果"
    assert [n.get_text() for n in doc.select('.math-inline')] == ['94.0/100', 'x_i']
    assert doc.code.get_text() == r'\(raw\)'


def test_summary_cannot_introduce_active_html_or_remote_images():
    doc = BeautifulSoup(summary_html('<script>alert(1)</script>\n\n'
        '<img src="https://example.com/tracker" onerror="alert(2)">\n\n'
        '[bad](javascript:alert(3)) ![remote](https://example.com/picture.png)\n\n$E=mc^2$'), 'html.parser')
    assert not doc.select('script,img,iframe,a')
    assert doc.select_one('.math-inline').get_text() == 'E=mc^2'
    assert not any(name.startswith('on') for node in doc.find_all(True) for name in node.attrs)
