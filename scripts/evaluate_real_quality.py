"""Snapshot five existing public-paper reports; optionally run a bounded live audit.

Default is local/read-only with respect to the source library. --live explicitly
uses the configured model for one text-only report and two comparisons. It never
invokes discovery, delivery, scheduling, Zotero or Obsidian.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import fitz

from arxiv_ra.cli import _load_dotenv
from arxiv_ra.comparison import ComparisonService
from arxiv_ra.config import load_config
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.llm import LLMClient
from arxiv_ra.model_budget import model_request_budget
from arxiv_ra.models import Author, Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.render import render_report
from arxiv_ra.report import ReportGenerator, finalize_report_structure
from arxiv_ra.utils import env, write_json


SAMPLES = ("2502.19453", "2506.03139", "2506.15903", "2509.13399", "2609.03806")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecordedLLM(LLMClient):
    def __init__(self, config, directory, timeout=120):
        super().__init__(config)
        if not self.enabled:
            raise RuntimeError("No configured model; local snapshot remains available")
        self.client = self.client.with_options(timeout=timeout, max_retries=0)
        self.directory = directory
        self.call_number = 0
        self.response_models = []

    def _completion(self, **kwargs):
        result = super()._completion(**kwargs)
        self.response_models.append(result.model)
        return result

    def chat(self, system, user, json_mode=False):
        self.call_number += 1
        stem = self.directory / f"call-{self.call_number:02d}"
        write_json(stem.with_suffix(".request.json"), {
            "system": system, "user": user, "json_mode": json_mode,
            "prompt_sha256": hashlib.sha256((system + "\0" + user).encode()).hexdigest(),
        })
        result = super().chat(system, user, json_mode=json_mode)
        stem.with_suffix(".response.txt").write_text(result, encoding="utf-8")
        return result


def prepare(source, output):
    if output.exists():
        raise ValueError("Use a new output directory; evaluation records are not overwritten")
    # Avoid creating evaluation artifacts inside the original library.
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Evaluation output must be separate from the source library")
    selected = []
    for aid in SAMPLES:
        paths = sorted(p for p in source.glob(f"????-??-??/reports/{aid}*/report.md")
                       if p.with_name("paper.pdf").is_file())
        if not paths:
            raise ValueError(f"No local report and PDF for {aid}")
        selected.append(paths[-1])
    output.mkdir(parents=True)
    records = []
    for path in selected:
        original = json.loads(path.with_name("metadata.json").read_text(encoding="utf-8"))
        paper = original["paper"]
        target = output / "library" / "2026-09-25" / "reports" / paper["arxiv_id"]
        target.mkdir(parents=True)
        files = {}
        for name in ("report.md", "paper.pdf", "metadata.json"):
            src = path.with_name(name)
            files[name] = {"path": src.as_posix(), "sha256": digest(src)}
            shutil.copy2(src, target / ("original-metadata.json" if name == "metadata.json" else name))
        # Explicit isolated evaluation scope; do not assign a historic quality/model.
        scoped = dict(original, profile_id="quality-evaluation")
        write_json(target / "metadata.json", scoped)
        with fitz.open(target / "paper.pdf") as pdf:
            pages = [page.get_text() for page in pdf]
        write_json(target / "pages.json", pages)
        records.append({"paper": {k: paper.get(k) for k in ("arxiv_id", "version", "title")},
                        "pdf_pages": len(pages), "files": files,
                        "evaluation_directory": target.relative_to(output).as_posix(),
                        "historic_model": original.get("model"),
                        "historic_prompt": "not_recorded"})
    write_json(output / "manifest.json", {
        "created_at": datetime.now(timezone.utc).isoformat(), "samples": records,
        "scope": "existing reports; targeted manual audit, not a random sample or model accuracy score",
        "adaptations": ["isolated profile assigned to copied metadata only",
                        "one optional text-only report; no figure/model image interpretation",
                        "comparisons use production prepare(); no manually injected PDF evidence"],
    })
    return records


def run_live(config_path, output, records, limit, *, model=None, api_key_env=None,
             base_url_env=None, comparisons_only=False, timeout=120):
    _load_dotenv(config_path.parent / ".env")
    config = load_config(config_path)
    config.output_dir = str(output / "library")
    config.profile_id = "quality-evaluation"
    config.profile_name = "真实论文质量验收"
    for name, value in (("model", model), ("api_key_env", api_key_env), ("base_url_env", base_url_env)):
        if value:
            setattr(config.llm, name, value)
    record = {"model": config.llm.model, "llm_config": asdict(config.llm),
              "endpoint_sha256": hashlib.sha256((env(config.llm.base_url_env) or "default").encode()).hexdigest(),
              "started_at": datetime.now(timezone.utc).isoformat(), "request_limit": limit,
              "steps": [], "status": "running", "comparisons_only": comparisons_only,
              "timeout_seconds": timeout}
    log = output / "live.json"
    write_json(log, record)
    llm = None
    with model_request_budget(limit) as budget:
        try:
            llm = RecordedLLM(config.llm, output, timeout=timeout)
            selected = next(r for r in records if r["paper"]["arxiv_id"] == "2506.15903")
            folder = output / selected["evaluation_directory"]
            metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            paper = Paper.from_dict(metadata["paper"])
            verified = dict(metadata.get("verified") or {})
            verified["authors"] = [Author(**a) for a in verified.get("authors", [])]
            verified = VerifiedMetadata(**verified)
            pages = json.loads((folder / "pages.json").read_text(encoding="utf-8"))
            parsed = ParsedPaper("\n\n".join(pages), pages, total_pages=len(pages))
            generator = ReportGenerator(llm, config.llm)
            calls_needed = 2 if comparisons_only else len(generator.analysis_inputs(parsed)[0]) + 1 + 2
            if calls_needed > limit:
                raise ValueError("Configured chunk size exceeds evaluation request limit")
            for sample in records:
                directory = output / sample["evaluation_directory"]
                render_report((directory / "report.md").read_text(encoding="utf-8"), directory / "report.html", sample["paper"]["title"])
            if not comparisons_only:
                print("Generating text-only VectorEdits report", flush=True)
                report = generator.generate(paper, verified, parsed, None)
                report, evidence = attach_evidence(finalize_report_structure(report, None), parsed,
                                                   pdf_available=True, full_report=True)
                # The original report remains in the source library and in this copy.
                (folder / "report.md").rename(folder / "historic-report.md")
                (folder / "report.md").write_text(report, encoding="utf-8")
                render_report(report, folder / "report.html", paper.title)
                write_json(folder / "evidence.json", evidence)
                write_json(folder / "metadata.json", dict(metadata, report_quality="full", evidence=evidence,
                                                           model=config.llm.model))
                record["steps"].append({"kind": "report", "path": (folder / "report.html").relative_to(output).as_posix()})
                write_json(log, record)
            groups = [(["2502.19453", "2506.15903"], "比较 SVG 编辑数据构建、实验指标与无编辑基线的适用边界；不作跨论文胜负排名。"),
                      (["2509.13399", "2609.03806"], "比较编辑评估与生成评估的任务定义、人工一致性验证和适用边界；不同任务的数值不可直接排名。")]
            service = ComparisonService(config, config_path.parent, clients=SimpleNamespace(llm=llm))
            for ids, question in groups:
                keys = [f"{r['paper']['arxiv_id']}v{r['paper']['version']}" for r in records if r["paper"]["arxiv_id"] in ids]
                print("Generating comparison: " + ", ".join(keys), flush=True)
                result = service.generate(service.prepare(keys, question))
                status = json.loads(result.with_name("metadata.json").read_text(encoding="utf-8"))["status"]
                record["steps"].append({"kind": "comparison", "path": result.relative_to(output).as_posix(), "status": status})
                write_json(log, record)
            record["status"] = "completed" if all(s.get("status", "model_assisted") == "model_assisted" for s in record["steps"]) else "incomplete"
        except Exception as exc:
            record["status"] = "failed"
            record["error_type"] = type(exc).__name__  # Never persist credential-bearing error text.
            print("Live evaluation stopped: " + type(exc).__name__, flush=True)
        finally:
            record.update(requests_used=budget.used, requests_blocked=budget.blocked,
                          response_models=llm.response_models if llm else [],
                          finished_at=datetime.now(timezone.utc).isoformat())
            write_json(log, record)
            if llm:
                llm.client.close()
    return record["status"] == "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--comparisons-only", action="store_true", help="Live mode: keep all historical reports")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", help="Environment variable name, never the key value")
    parser.add_argument("--base-url-env", help="Environment variable name")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-requests", type=int, default=5, choices=range(1, 6))
    args = parser.parse_args()
    output = args.output.resolve()
    records = prepare(args.source.resolve(), output)
    print(f"Snapshotted {len(records)} reports and PDFs", flush=True)
    success = run_live(args.config.resolve(), output, records, args.max_requests, model=args.model,
                       api_key_env=args.api_key_env, base_url_env=args.base_url_env,
                       comparisons_only=args.comparisons_only, timeout=args.timeout_seconds) if args.live else True
    unchanged = all(digest(Path(f["path"])) == f["sha256"] for r in records for f in r["files"].values())
    write_json(output / "source-integrity.json", {"all_original_files_unchanged": unchanged})
    return 0 if success and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
