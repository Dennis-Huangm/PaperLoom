from paperloom.sources.arxiv_html import parse_html_figure_specs
import pytest


@pytest.mark.parametrize('heading', ['4 Experiments on ImageNet', '4 Results and Analysis',
                                  'Experimental Evaluation on Benchmarks'])
def test_descriptive_experiment_headings_keep_figures_in_results(heading):
    html = (f'<h2>{heading}</h2><figure class="ltx_figure"><img src="f9.png">'
            '<figcaption>Figure 9: Benchmark results.</figcaption></figure>')
    figures = parse_html_figure_specs(html, 'https://arxiv.org/html/example')
    assert [figure.role for figure in figures] == ['experiment']


def test_parse_html_figures_preserves_experiments_and_split_caption_before_appendix() -> None:
    html = """
    <html><body>
      <h2>1 Introduction</h2>
      <figure class="ltx_figure" id="S1.F1">
        <img src="paper/figure1.png" alt="Refer to caption">
        <figcaption>Figure 1: Complete overview.</figcaption>
      </figure>
      <figure class="ltx_figure" id="S2.F2">
        <figure class="ltx_figure ltx_figure_panel"><img src="paper/a.png"></figure>
        <figure class="ltx_figure ltx_figure_panel"><img src="paper/b.png"></figure>
        <figcaption>Figure 2: Split panels.</figcaption>
      </figure>
      <h2>3 Experiments</h2>
      <figure class="ltx_figure"><img src="paper/result.png"><figcaption>Figure 3: Results.</figcaption></figure>
      <h2>Appendix A</h2>
      <figure class="ltx_figure"><img src="extra.png"><figcaption>Figure 4: Supplement.</figcaption></figure>
    </body></html>
    """

    specs = parse_html_figure_specs(html, "https://arxiv.org/html/1234.56789")

    assert [spec.number for spec in specs] == [1, 2, 3]
    assert [spec.role for spec in specs] == ["method", "method", "experiment"]
    assert specs[1].image_url == ""
    assert specs[0].number == 1
    assert specs[0].image_url == "https://arxiv.org/html/paper/figure1.png"


def test_parse_html_figures_uses_single_complete_image_from_nested_panels() -> None:
    html = """
    <h2>1 Method</h2>
    <figure class="ltx_figure">
      <img src="1234.56789v1/combined.png">
      <figure class="ltx_figure ltx_figure_panel"><figcaption>(a) Part A.</figcaption></figure>
      <figcaption>Figure 1: Combined method.</figcaption>
    </figure>
    <h2>2 Experiments</h2>
    """

    specs = parse_html_figure_specs(html, "https://arxiv.org/html/1234.56789")

    assert [spec.number for spec in specs] == [1]


def test_lettered_and_structural_appendices_are_excluded() -> None:
    for appendix in (
        '<h2>A Additional Results</h2>',
        '<section class="ltx_appendix"><h2>More cases</h2>',
    ):
        html = ('<h2>4 Experiments</h2><figure class="ltx_figure"><img src="f5.png">'
                '<figcaption>Figure 5: Results.</figcaption></figure>' + appendix
                + '<figure class="ltx_figure"><img src="f6.png">'
                '<figcaption>Figure 6: Supplement.</figcaption></figure>')
        assert [s.number for s in parse_html_figure_specs(html, "https://arxiv.org/html/example")] == [5]


def test_document_title_starting_with_a_is_not_an_appendix() -> None:
    html = ('<h1 class="ltx_title_document">A Unified Framework for Editing</h1>'
            '<h2>3 Experiments</h2><figure class="ltx_figure"><img src="f5.png">'
            '<figcaption>Figure 5: Results.</figcaption></figure>')
    assert [s.number for s in parse_html_figure_specs(html, "https://arxiv.org/html/example")] == [5]
