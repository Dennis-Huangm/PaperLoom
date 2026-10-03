"""Serve disposable local reports for report_smoke.cjs; no user data or services."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import uvicorn

from arxiv_ra.render import render_report
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app


fixture_directory = TemporaryDirectory(prefix="paperloom-report-preview-")
root = Path(fixture_directory.name)
config = root / "config.yaml"
config.write_text("output_dir: run\ndiscovery:\n  interest_description: preview\n", encoding="utf-8")
for number, title in enumerate([
    "SVGEval: A Vision-Grounded Framework for Perceptual-Quality Benchmarking and Evaluation in Text-to-SVG Generation",
    "Visual Agents: Learning to Reason with Feedback",
    "稀疏奖励与策略优化：研究记录",
]):
    directory = root / "run" / "2026-10-03" / "reports" / f"paper-{number}"
    directory.mkdir(parents=True)
    aid = f"2608.{1977 + number:05}"
    write_json(directory / "metadata.json", {
        "profile_id": "preview", "profile_name": "图像生成",
        "paper": {"arxiv_id": aid, "version": 1, "title": title, "authors": [{"name": "Ada"}]},
    })
    text = f"# {title}\n\n| 字段 | 内容 |\n|---|---|\n| arXiv ID | {aid} |\n| 作者 | Ada |\n\n"
    for section in ["一句话总结", "核心方法", "关键结果", "局限性", *[f"补充分析 {index}" for index in range(1, 31)]]:
        text += f"## {section}\n\n" + "这是用于验证报告阅读布局的独立演示材料。" * 18 + "\n\n"
    (directory / "report.md").write_text(text, encoding="utf-8")
    render_report(text, directory / "report.html", title, aid)
print(f"REPORT_PREVIEW_READY {root}", flush=True)
uvicorn.run(create_app(config), host="127.0.0.1", port=int(os.environ.get("REPORT_PREVIEW_PORT", "8771")), log_level="warning")
