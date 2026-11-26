"""Disposable report library for report-directory.cjs; no external services."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import uvicorn

from paperloom.utils import write_json
from paperloom.web import create_app


directory = TemporaryDirectory(prefix="paperloom-report-directory-")
root = Path(directory.name)
config = root / "config.yaml"
config.write_text("output_dir: run\ndiscovery:\n  interest_description: preview\n", encoding="utf-8")
titles = [
    "MoVer: Motion Verification for Motion Graphics Animations",
    "SketchVLM: Vision language models can annotate images to explain thoughts and guide users",
    "VectorHarness: Recovering Editable, Relation-Preserving Structure from Scientific Graphics",
    "WildSVG: Towards Reliable SVG Generation Under Real-World Conditions",
    '中文长标题与边界字符 <script>example()</script> & "SVG"：' + "多轮图形编辑与视觉反馈" * 10,
]
for i in range(32):
    folder = root / "run" / f"2026-10-{31 - i // 2:02}" / "reports" / f"paper-{i:02}"
    title = titles[i] if i < len(titles) else f"SVG Agents {i + 1}: Learning Structured Editing with Visual Feedback"
    write_json(folder / "metadata.json", {
        "profile_id": "alpha" if i % 2 == 0 else "beta",
        "profile_name": "图形生成" if i % 2 == 0 else "视觉反馈",
        "report_quality": "full",
        "generated_at": f"2026-10-{31 - i // 2:02}T{20 - i % 2:02}:00:00+00:00",
        "paper": {"arxiv_id": f"2608.{1977 + i:05}", "version": 2,
                  "published": f"2026-08-{i % 28 + 1:02}T00:00:00Z" if i < 30 else "",
                  "title": title, "authors": [{"name": "Ada Researcher"}]},
    })
    (folder / "report.html").write_text("<h1>Fixture report</h1>", encoding="utf-8")

if __name__ == "__main__":
    uvicorn.run(create_app(config), host="127.0.0.1",
                port=int(os.environ.get("REPORT_CATALOG_PREVIEW_PORT", "8794")), log_level="warning")
