"""Run reproducible synthetic quality-guard checks without network or model calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from arxiv_ra.comparison import ComparisonService
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


def evaluate(case):
    if case["type"] == "report":
        _, evidence = attach_evidence(case["report"], ParsedPaper("", case["pages"], total_pages=len(case["pages"])),
                                      pdf_available=True, full_report=True)
        actual = {"flagged": bool(evidence.get("numeric_audit", {}).get("issues")),
                  "citations": evidence["validated_citations"]}
    else:
        ref = "P2:A" if case.get("wrong_ref") else "P1:A"
        cell = {"text": case.get("text", ""), "kind": case.get("kind", "source"), "evidence": [ref]}
        if "support" in case:
            cell["support"] = [{"evidence": ref, "quote": case["support"]}]
        snapshot = {"sources": [{"id": "P1", "evidence": [{"id": "P1:A", "kind": case.get("source_kind", "abstract"), "text": case["source"]}]}]}
        payload = {"papers": [{"id": "P1", "dimensions": {} if case.get("omit_cell") else {"关键结果": cell}}]}
        matrix, _ = ComparisonService._matrix(snapshot, payload)
        actual = {"kind": matrix["P1"]["关键结果"]["kind"]}
    return {"id": case["id"], "type": case["type"], "actual": actual,
            "expected": case.get("expected"), "manual_review": case.get("manual_review"),
            "passed": actual == case["expected"] if "expected" in case else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/quality/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    cases = json.loads(raw)["cases"]
    results = [evaluate(case) for case in cases]
    scored = [r for r in results if r["passed"] is not None]
    record = {"generated_at": datetime.now(timezone.utc).isoformat(), "suite_sha256": hashlib.sha256(raw).hexdigest(),
              "scope": "synthetic offline guard checks, not model accuracy or semantic entailment evaluation",
              "passed": sum(r["passed"] for r in scored), "scored": len(scored),
              "manual_review": len(results) - len(scored), "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{record['passed']}/{record['scored']} offline checks passed; {record['manual_review']} manual-review cases")
    return 0 if all(r["passed"] for r in scored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
