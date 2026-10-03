"""Private, disposable checkpoints shared only by an explicit report retry chain."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from .models import FigureCandidate, ParsedPaper
from .task_runtime import task_checkpoint, task_checkpoint_data, task_warning, task_progress
from .utils import read_json, write_json


class CheckpointWriteError(RuntimeError):
    """Do not hide a failed checkpoint write behind an abstract-only fallback."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


_CURRENT: ContextVar[ReportCheckpoint | None] = ContextVar("report_checkpoint", default=None)


def current_report_checkpoint():
    return _CURRENT.get()


@contextmanager
def bind_report_checkpoint(checkpoint):
    token = _CURRENT.set(checkpoint)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class ReportCheckpoint:
    def __init__(self, output_root, config, paper, resume=None):
        base = Path(output_root).resolve() / ".jobs" / "report-work"
        identity = fingerprint({"version": 1, "profile": config.profile_id,
                                "paper": paper.to_dict()})
        self.root = None
        if resume:
            try:
                identifier = resume["id"]
                if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                    raise ValueError("invalid checkpoint ID")
                candidate = base / identifier
                candidate.resolve().relative_to(base.resolve())
                if read_json(candidate / "identity.json") != {"identity": identity}:
                    raise ValueError("identity changed")
                self.root = candidate
            except (OSError, ValueError, TypeError, KeyError):
                task_warning("报告恢复", "原中间结果缺失、损坏或论文信息不兼容，已开始新的全文处理；旧文件保留。")
        if self.root is None:
            self.root = base / uuid.uuid4().hex
            self.root.mkdir(parents=True, exist_ok=True)
            self._write(self.root / "identity.json", {"identity": identity})
        self.pdf_config = asdict(config.pdf)
        self.source = ""
        self.model_identity = None
        self.state = {"parsed": False, "chunks_done": 0, "chunks_total": 0,
                      "report_ready": False, "resumable": True}
        task_checkpoint_data("report_work", {"id": self.root.name})
        self.publish()

    @staticmethod
    def _write(path, value):
        try:
            write_json(path, value)
        except OSError as exc:
            raise CheckpointWriteError("无法保存报告中间结果，请检查磁盘空间及目录权限") from exc

    def publish(self, **values):
        self.state.update(values)
        try:
            task_checkpoint_data("report_progress", self.state)
        except OSError as exc:
            raise CheckpointWriteError("无法保存报告恢复进度，请检查磁盘空间及目录权限") from exc

    def _file(self, name):
        # Checkpoint paths remain portable across moves and cannot escape storage.
        path = self.root / name
        path.resolve().relative_to(self.root.resolve())
        return path

    def restore_parsed(self, target, local_pdf=None):
        path = self.root / "parsed.json"
        if not path.exists():
            return None
        try:
            data = read_json(path)
            if data["config"] != self.pdf_config or data["version"] != 1:
                raise ValueError("parser settings changed")
            for name, digest in data["files"].items():
                if file_digest(self._file(name)) != digest:
                    raise ValueError("source changed")
            if local_pdf and file_digest(local_pdf) != data["files"]["paper.pdf"]:
                raise ValueError("local PDF changed")
            raw = data["parsed"]
            if not isinstance(raw["text"], str) or not raw["text"].strip():
                raise ValueError("invalid parsed text")
            if fingerprint(raw) != data["parsed_hash"]:
                raise ValueError("parsed text changed")
            figures = [FigureCandidate(path=target / item["path"], **{k: v for k, v in item.items() if k != "path"})
                       for item in raw["figures"]]
            for item in raw["figures"]:
                if item["path"] not in data["files"]:
                    raise ValueError("unverified figure")
            parsed = ParsedPaper(text=raw["text"], page_texts=raw["page_texts"],
                                 parser=raw["parser"], total_pages=raw["total_pages"], figures=figures)
            for name in data["files"]:
                destination = target / name
                destination.resolve().relative_to(target.resolve())
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._file(name), destination)
            self.source = fingerprint([self.pdf_config, data["files"]["paper.pdf"], data["parsed_hash"]])
            self.publish(parsed=True)
            return parsed
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            task_warning("报告恢复", "PDF、解析设置或中间文件已变化/损坏，将重新下载或解析并校验分片；旧报告保留。")
            return None

    def save_parsed(self, parsed, pdf_path):
        try:
            self._save_parsed(parsed, pdf_path)
        except OSError as exc:
            raise CheckpointWriteError("无法保存 PDF 解析中间结果，请检查磁盘空间及目录权限") from exc

    def _save_parsed(self, parsed, pdf_path):
        raw = asdict(parsed)
        files = {"paper.pdf": file_digest(pdf_path)}
        shutil.copy2(pdf_path, self.root / "paper.pdf")
        for index, figure in enumerate(parsed.figures):
            name = f"figures/{index}{figure.path.suffix}"
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(figure.path, destination)
            files[name] = file_digest(destination)
            raw["figures"][index]["path"] = name
        self._write(self.root / "parsed.json", {"version": 1, "config": self.pdf_config,
                    "files": files, "parsed": raw, "parsed_hash": fingerprint(raw)})
        self.source = fingerprint([self.pdf_config, files["paper.pdf"], fingerprint(raw)])
        self.publish(parsed=True)

    def configure_model(self, config, llm):
        # Hash the effective endpoint; never store its credentials or runtime key.
        identity = fingerprint([asdict(config), str(getattr(getattr(llm, "client", None), "base_url", "")), self.source])
        try:
            previous = read_json(self.root / "model.json", {})
        except (OSError, ValueError):
            previous = {"invalid": True}
        if previous and previous != {"identity": identity}:
            task_warning("报告恢复", "模型、服务地址、分片设置或源材料已变化，旧模型分析不再复用。")
        self.model_identity = identity
        self._write(self.root / "model.json", {"identity": identity})

    def signature(self, inputs):
        return fingerprint([self.model_identity, inputs])

    def get(self, key, inputs):
        task_checkpoint()
        try:
            data = read_json(self.root / f"{key}.json", {})
            if not data:
                return None
            if data["signature"] != self.signature(inputs):
                task_progress("部分模型输入或提示词已变化，对应步骤将重新生成。")
                return None
            if (not isinstance(data["text"], str) or not data["text"].strip()
                    or fingerprint(data["text"]) != data["hash"]):
                raise ValueError("invalid result")
            return data["text"]
        except (OSError, ValueError, TypeError, KeyError):
            task_warning("报告恢复", "部分中间分析无法校验，对应步骤将重新生成。")
            return None

    def put(self, key, inputs, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("模型返回空内容，未保存为已完成步骤")
        # Preserve a completed response even if cancellation arrived in flight.
        self._write(self.root / f"{key}.json", {"signature": self.signature(inputs),
                    "text": text, "hash": fingerprint(text)})
