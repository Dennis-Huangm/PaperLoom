from pathlib import Path
import re

import pytest

from arxiv_ra.config import AppConfig, ObsidianConfig
from arxiv_ra.obsidian import MANAGED_END, ObsidianError, ObsidianExporter
from arxiv_ra.utils import read_json, write_json


def _paper() -> dict:
    return {
        "arxiv_id": "2407.05600",
        "title": "GenArtist: Multimodal LLM as an Agent",
        "authors": [{"name": "Zhenyu Wang"}, {"name": "Aoxue Li"}],
        "abstract": "An agentic image generation system with visual feedback.",
        "categories": ["cs.CV", "cs.AI"],
        "primary_category": "cs.CV",
        "published": "2024-07-08T00:00:00+00:00",
        "updated": "2024-10-28T00:00:00+00:00",
        "abs_url": "https://arxiv.org/abs/2407.05600",
        "pdf_url": "https://arxiv.org/pdf/2407.05600",
        "final_score": 9.4,
        "recommendation_reason": "与 AgenticT2I 高度相关。",
    }


def _exporter(tmp_path: Path, **overrides) -> ObsidianExporter:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = ObsidianConfig(
        enabled=True,
        vault_path=str(vault),
        **overrides,
    )
    config = AppConfig(
        output_dir=str(tmp_path / "run"),
        profile_id="agentict2i",
        profile_name="AgenticT2I",
        obsidian=settings,
    )
    config.discovery.positive_keywords = [
        "agentic image generation",
        "visual feedback",
    ]
    config.discovery.interest_description = "Agentic image generation and editing."
    return ObsidianExporter(config, tmp_path)


def test_sync_builds_daily_paper_concept_and_indexes(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path)
    recommendation = {
        "profile_id": "agentict2i",
        "paper": _paper(),
        "verified": {"venue": "NeurIPS 2024"},
    }

    daily = exporter.sync_daily("2026-08-22", [recommendation])

    root = tmp_path / "vault" / "arXiv Research Assistant"
    manifest = read_json(root / ".arxiv-ra-manifest.json", {})
    paper_note = tmp_path / "vault" / f"{manifest['papers']['2407.05600']['note']}.md"
    assert daily.exists()
    assert paper_note.exists()
    assert (root / "Home" / "Research Hub.md").exists()
    assert (root / "System" / "Indexes" / "Papers.md").exists()
    assert (root / "System" / "Indexes" / "Concepts.md").exists()
    assert (root / "Topics" / "agentic image generation.md").exists()
    assert paper_note.name == "GenArtist.md"
    assert "[[arXiv Research Assistant/Papers/AgenticT2I/GenArtist|" in daily.read_text(encoding="utf-8")
    paper_index = (root / "System" / "Indexes" / "Papers.md").read_text(
        encoding="utf-8"
    )
    assert "[[arXiv Research Assistant/Papers/AgenticT2I/GenArtist\\|GenArtist:" in paper_index
    paper_row = next(line for line in paper_index.splitlines() if "GenArtist" in line)
    assert len(re.split(r"(?<!\\)\|", paper_row)) == 7
    assert "type: paper-note" in paper_note.read_text(encoding="utf-8")


def test_full_sync_reads_active_profiles_same_day_recommendations(
    tmp_path: Path,
) -> None:
    exporter = _exporter(tmp_path)
    date_dir = tmp_path / "run" / "2026-08-22"
    agentic = _paper()
    robotics = {**_paper(), "arxiv_id": "2608.00002", "title": "Robot Policy"}
    write_json(
        date_dir / "recommendations-agentict2i.json",
        [{"profile_id": "agentict2i", "paper": agentic, "verified": {}}],
    )
    write_json(
        date_dir / "recommendations.json",
        [{"profile_id": "robotics", "paper": robotics, "verified": {}}],
    )

    exporter.sync_all()

    manifest = read_json(
        tmp_path
        / "vault"
        / "arXiv Research Assistant"
        / ".arxiv-ra-manifest.json",
        {},
    )
    assert "2407.05600" in manifest["papers"]
    assert "2608.00002" not in manifest["papers"]


def test_report_sync_copies_images_and_preserves_user_notes(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path)
    report_dir = tmp_path / "run" / "2026-08-22" / "reports" / "2407.05600-genartist"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.md"
    report.write_text(
        "# GenArtist\n\n## 摘要级主要内容\n\nAn agentic image generation system with visual feedback.\n\n## 核心方法\n\n![方法图](method-figure-01.png)\n\n完整报告内容。\n",
        encoding="utf-8",
    )
    (report_dir / "method-figure-01.png").write_bytes(b"image-bytes")
    write_json(
        report_dir / "metadata.json",
        {"paper": _paper(), "verified": {"venue": "NeurIPS 2024"}},
    )

    note = exporter.sync_report(_paper(), {"venue": "NeurIPS 2024"}, report)
    first = note.read_text(encoding="utf-8")
    assert "![[arXiv Research Assistant/Attachments/2407.05600/method-figure-01.png]]" in first
    assert "完整报告内容" in first
    assert "## 精炼摘要" in first
    assert "An agentic image generation system with visual feedback." not in first
    assert "摘要级主要内容" not in first
    assert (tmp_path / "vault" / "arXiv Research Assistant" / "Attachments" / "2407.05600" / "method-figure-01.png").exists()
    assert "[[arXiv Research Assistant/Topics/agentic image generation\\|agentic image generation]]" in first

    note.write_text(first + "\n我的永久补充。\n", encoding="utf-8")
    report.write_text(report.read_text(encoding="utf-8") + "\n新增报告内容。\n", encoding="utf-8")
    exporter.sync_feedback(_paper(), {"label": "必读", "verdict": "must_read"}, {"venue": "NeurIPS 2024"})
    updated = note.read_text(encoding="utf-8")
    assert "新增报告内容" in updated
    assert "我的永久补充" in updated
    assert updated.index("我的永久补充") > updated.index(MANAGED_END)


def test_unmanaged_existing_note_is_never_overwritten(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path)
    expected = (
        tmp_path
        / "vault"
        / "arXiv Research Assistant"
        / "Papers"
        / "AgenticT2I"
        / "GenArtist.md"
    )
    expected.parent.mkdir(parents=True)
    expected.write_text("USER OWNED", encoding="utf-8")

    note = exporter.sync_feedback(_paper(), {"label": "相关", "verdict": "relevant"})

    assert expected.read_text(encoding="utf-8") == "USER OWNED"
    assert note != expected
    assert "(arXiv RA)" in note.name
    assert note.exists()


def test_paper_note_uses_method_or_readable_title_without_arxiv_id(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path)

    named = exporter.sync_feedback(_paper(), {"label": "相关", "verdict": "relevant"})
    generic_paper = _paper()
    generic_paper.update(
        {
            "arxiv_id": "2608.04436",
            "title": "A Survey of Agentic Image Generation Systems",
        }
    )
    generic = exporter.sync_feedback(
        generic_paper, {"label": "相关", "verdict": "relevant"}
    )

    assert named.name == "GenArtist.md"
    assert generic.name == "A Survey of Agentic Image Generation Systems.md"
    assert "2407.05600" not in named.name
    assert "2608.04436" not in generic.name


def test_output_folder_cannot_escape_vault(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path, root_folder="../escape")

    with pytest.raises(ObsidianError, match="安全的相对目录"):
        exporter.sync_all()


def test_missing_report_and_reason_uses_cached_llm_summary(tmp_path: Path, monkeypatch) -> None:
    exporter = _exporter(tmp_path)
    paper = _paper()
    paper["recommendation_reason"] = ""
    calls = []
    exporter.llm.enabled = True
    monkeypatch.setattr(
        exporter.llm,
        "chat",
        lambda system, user: calls.append(user)
        or "该工作研究智能体图像生成，通过视觉反馈迭代修正结果。摘要报告方法可提升一致性，但没有说明完整泛化边界。",
    )

    note = exporter.sync_feedback(
        paper, {"label": "相关", "verdict": "relevant"}
    )
    exporter.sync_feedback(paper, {"label": "相关", "verdict": "relevant"})

    text = note.read_text(encoding="utf-8")
    assert "## 精炼摘要" in text
    assert "视觉反馈迭代修正" in text
    assert paper["abstract"] not in text
    assert len(calls) == 1


def test_report_math_uses_obsidian_delimiters_without_touching_code(tmp_path: Path) -> None:
    exporter = _exporter(tmp_path)
    report_dir = tmp_path / "run" / "2026-08-22" / "reports" / "2407.05600-genartist"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.md"
    report.write_text(
        """# GenArtist

## 核心方法

行内公式为 \\(E=mc^2\\)。

\\[
S_{\\mathrm{diff}} = \\alpha N(\\Delta L)
\\]

行内代码 `print(r"\\(not-math\\)")` 不应转换。

```python
formula = r"\\[also-not-math\\]"
```
""",
        encoding="utf-8",
    )

    note = exporter.sync_report(_paper(), {}, report)
    text = note.read_text(encoding="utf-8")

    assert "$E=mc^2$" in text
    assert "$$\nS_{\\mathrm{diff}} = \\alpha N(\\Delta L)\n$$" in text
    assert '`print(r"\\(not-math\\)")`' in text
    assert 'formula = r"\\[also-not-math\\]"' in text
