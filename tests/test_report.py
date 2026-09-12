from pathlib import Path

from arxiv_ra.models import FigureCandidate
from arxiv_ra.report import CORE_METHOD_GUIDANCE, ReportGenerator, finalize_report_structure


def test_normalize_title_replaces_literal_prompt_placeholder() -> None:
    report = "# 原始英文标题\n\n## 一句话总结\n内容"

    normalized = ReportGenerator._normalize_title(report, "Verified Paper Title")

    assert normalized.startswith("# Verified Paper Title\n")
    assert "# 原始英文标题" not in normalized


def test_normalize_title_preserves_model_supplied_title() -> None:
    report = "# Existing Paper Title\n\n## 一句话总结\n内容"

    assert ReportGenerator._normalize_title(report, "Verified Paper Title") == report


def test_finalize_report_moves_figure_into_core_method_and_removes_unwanted_sections() -> None:
    report = """# Paper

## 核心方法

方法正文。

![论文候选主图](main-figure.png)

## 主图说明

该图展示规划、工具调用与视觉反馈之间的信息流。

## 阅读建议

先读方法。

## 核验备注

元数据说明。
"""
    figure = FigureCandidate(Path("main-figure.png"), 2, "Figure 1: Overview", score=9)

    final = finalize_report_structure(report, figure)

    assert "## 主图说明" not in final
    assert "## 阅读建议" not in final
    assert "## 核验备注" not in final
    assert "## 核心方法\n\n### 方法图解析" in final
    assert "![论文方法图 Figure 1](main-figure.png)" in final
    assert "该图展示规划、工具调用与视觉反馈之间的信息流。" in final


def test_finalize_report_silently_drops_figure_content_without_reliable_figure() -> None:
    report = """# Paper

## 核心方法

方法正文。

## 主图说明

未可靠提取到论文主图。

## 阅读建议

建议内容。
"""

    final = finalize_report_structure(report, None)

    assert "主图" not in final
    assert "阅读建议" not in final
    assert "方法正文" in final


def test_finalize_method_figure_group_is_idempotent() -> None:
    report = "# Paper\n\n## 核心方法\n\n方法正文。\n"
    figures = [
        FigureCandidate(Path("method-figure-01.png"), 2, "Figure 1: Overview", kind="page_figure"),
        FigureCandidate(Path("method-figure-02.png"), 4, "Figure 2: Pipeline", kind="page_figure"),
    ]

    once = finalize_report_structure(report, figures)
    twice = finalize_report_structure(once, figures)

    assert twice.count("### 方法图解析") == 1
    assert twice.count("#### Figure 1") == 1
    assert twice.count("#### Figure 2") == 1
    assert twice.count("![论文方法图") == 2


def test_figure_explanation_is_used_instead_of_original_caption() -> None:
    report = "# Paper\n\n## 核心方法\n\n方法正文。\n"
    figure = FigureCandidate(
        Path("method-figure-01.png"),
        0,
        "Figure 1: Original caption must not be copied.",
        kind="html_figure",
        explanation="这张图从输入图像开始，经过编码与渲染，最终得到可用于推理的视觉表示。",
    )

    final = finalize_report_structure(report, [figure])

    assert "Original caption must not be copied" not in final
    assert "这张图从输入图像开始" in final
    assert "Figure 1 · arXiv HTML" not in final


def test_finalize_removes_legacy_figure_source_metadata() -> None:
    report = """# Paper

## 核心方法

### 方法图解析

#### Figure 2

![论文方法图 Figure 2](method-figure-02.png)

**Figure 2 · arXiv HTML**

**通俗解读**

方法说明。
"""

    figure = FigureCandidate(
        Path("method-figure-02.png"),
        0,
        "Figure 2: Method",
        kind="html_figure",
        explanation="方法说明。",
    )
    final = finalize_report_structure(report, [figure])

    assert "Figure 2 · arXiv HTML" not in final
    assert "**通俗解读**" in final


def test_core_method_guidance_requires_detailed_formula_explanation() -> None:
    assert "定义所有主要符号" in CORE_METHOD_GUIDANCE
    assert "计算顺序与输入输出" in CORE_METHOD_GUIDANCE
    assert "训练信号、推理决策或最终结果" in CORE_METHOD_GUIDANCE
    assert "不得根据常识虚构" in CORE_METHOD_GUIDANCE


def test_finalize_removes_model_authored_duplicate_figure_sections() -> None:
    report = """# ToolArtist

## 核心方法

### 总体范式

模型统一完成检索、推理和绘图。

### Figure 1：能力展示

这是一段由报告模型重复生成的逐图讲解。

### Figure 2：流程对比

这也是重复讲解，最终报告不应保留。

### 训练方法

通过监督微调和强化学习训练。

## 主要贡献

贡献正文。
"""
    figures = [
        FigureCandidate(
            Path("method-figure-01.png"),
            0,
            "Figure 1: Demo",
            kind="html_figure",
            explanation="统一定稿器生成的 Figure 1 通俗解读。",
        ),
        FigureCandidate(
            Path("method-figure-02.png"),
            0,
            "Figure 2: Pipeline",
            kind="html_figure",
            explanation="统一定稿器生成的 Figure 2 通俗解读。",
        ),
    ]

    final = finalize_report_structure(report, figures)

    assert final.count("### 方法图解析") == 1
    assert final.count("#### Figure 1") == 1
    assert final.count("#### Figure 2") == 1
    assert "重复生成的逐图讲解" not in final
    assert "这也是重复讲解" not in final
    assert "### 总体范式" in final
    assert "### 训练方法" in final
    assert "\n\n## 主要贡献\n\n" in final


def test_finalize_uses_one_canonical_top_level_sequence() -> None:
    report = """# Paper

## 关键结果

结果。

## 核心方法

方法。

## 一句话总结

总结。
"""

    final = finalize_report_structure(report, None)

    assert final.index("## 一句话总结") < final.index("## 核心方法")
    assert final.index("## 核心方法") < final.index("## 关键结果")
