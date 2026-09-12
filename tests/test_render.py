from pathlib import Path

from arxiv_ra.render import markdown_with_math, render_report


def test_markdown_math_preserves_display_latex() -> None:
    source = r"""## 核心方法

\[
V \xrightarrow{\psi} C \xrightarrow{\text{render}} \tilde V
\]
"""

    body, toc = markdown_with_math(source)

    assert 'class="math-block"' in body
    assert r"\xrightarrow{\psi}" in body
    assert "核心方法" in toc
    assert "<p>[" not in body


def test_render_report_includes_local_katex_and_sidebar_navigation(tmp_path: Path) -> None:
    destination = tmp_path / "report.html"

    render_report(
        "# Paper\n\n## 核心方法\n\n内容",
        destination,
        "Paper",
        arxiv_id="2407.05600",
    )

    page = destination.read_text(encoding="utf-8")
    assert "/static/vendor/katex/katex.min.js" in page
    assert "/static/vendor/katex/katex.min.css" in page
    assert 'class="report-sidebar"' in page
    assert 'aria-label="报告目录"' in page
    assert "返回报告库" in page
    assert 'id="report-library-add"' in page
    assert 'data-arxiv-id="2407.05600"' in page
    assert "/api/library/status" in page
    assert "/api/library/add" in page
    assert "scroll-padding-top:24px" in page
    assert "position:relative;z-index:10" in page
    assert "position:sticky;top:18px" in page
    assert "max-width:1920px" in page
    assert "grid-template-columns:270px minmax(0,1fr)" in page
    assert "window.scrollTo(0,0)" in page
