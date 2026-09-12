from arxiv_ra.arxiv_html import parse_html_figure_specs


def test_parse_html_figures_stops_before_experiments_and_skips_split_images() -> None:
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
    </body></html>
    """

    specs = parse_html_figure_specs(html, "https://arxiv.org/html/1234.56789")

    assert len(specs) == 1
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
