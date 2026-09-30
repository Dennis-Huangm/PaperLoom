"""Refresh the table inventory and HTML of an already edited local report."""
from __future__ import annotations

import argparse
from pathlib import Path

from arxiv_ra.evidence import report_table_coverage, source_table_index, source_table_inventory
from arxiv_ra.models import ParsedPaper
from arxiv_ra.render import render_report
from arxiv_ra.utils import atomic_write_text, read_json, write_json


def refresh(report_dir: Path, parsed_file: Path) -> dict:
    report_dir = report_dir.resolve()
    report_path = report_dir / "report.md"
    metadata_path = report_dir / "metadata.json"
    evidence_path = report_dir / "evidence.json"
    if not (report_dir / "paper.pdf").is_file():
        raise FileNotFoundError("report PDF is missing")
    report = report_path.read_text(encoding="utf-8").split("\n## 原文表格索引", 1)[0].rstrip()
    metadata = read_json(metadata_path)
    evidence = read_json(evidence_path)
    parsed_data = read_json(parsed_file)["parsed"]
    parsed = ParsedPaper("", parsed_data["page_texts"], total_pages=parsed_data["total_pages"])
    coverage = report_table_coverage(report, source_table_inventory(parsed))
    index = source_table_index(coverage)
    if index:
        report += "\n\n" + index
    report += "\n"
    evidence["table_coverage"] = coverage
    metadata["evidence"]["table_coverage"] = coverage
    atomic_write_text(report_path, report)
    write_json(evidence_path, evidence)
    write_json(metadata_path, metadata)
    render_report(report, report_dir / "report.html", metadata["paper"]["title"],
                  arxiv_id=metadata["paper"]["arxiv_id"],
                  profile_id=metadata["profile_id"],
                  report_id=(report_dir / "report.html").relative_to(report_dir.parents[2]).as_posix())
    return coverage


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path)
    parser.add_argument("parsed_file", type=Path)
    args = parser.parse_args()
    result = refresh(args.report_dir, args.parsed_file)
    print(f"Source tables: {result['source_count']}; shown: {result['presented']}; missing: {len(result['missing'])}")
