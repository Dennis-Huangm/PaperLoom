"""Serializable requests for explicit recovery; never deserialize executable code."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig


RECOVERABLE = {"report", "digest", "weekly", "compare", "version-batch"}


def capture_request(kind, config, project_root, **parameters):
    return {"version": 1, "kind": kind, "config": asdict(config),
            "project_root": str(project_root.resolve()), "parameters": deepcopy(parameters)}


def restore_config(request, output_root):
    if request.get("version") != 1 or request.get("kind") not in RECOVERABLE:
        raise ValueError("此任务不支持恢复，请从原功能入口重新提交")
    config = AppConfig()
    for field in fields(config):
        if field.name in request["config"]:
            value = deepcopy(request["config"][field.name])
            previous = getattr(config, field.name)
            setattr(config, field.name, type(previous)(**value) if is_dataclass(previous) else value)
    config.output_dir = str(output_root.resolve())
    return config


def recovery_note(kind):
    if kind == "version-batch":
        return "继续原批次，复用已成功的同步步骤。"
    note = "按保存的配置重新执行，可能再次调用模型及已配置的同步。"
    if kind == "digest":
        return note + "重新检索并应用当前阅读偏好；恢复不发送邮件。"
    if kind == "weekly":
        return note + "保留原统计截止时间，重新读取可确认的本地活动和资料。"
    if kind == "report":
        return "手动继续全文分析，校验后复用已保存的解析、分片和整合结果；不兼容步骤重新生成。论文版本固定，尚未解析的无版本 ID 会重新查询。已配置同步可能再次执行。"
    if kind == "compare":
        return note + "使用原比较材料快照。"
    return "此类任务请从原功能入口继续；已有任务记录和结果仍保留。"


def recovery_runner(request, output_root, checkpoint):
    config = restore_config(request, output_root)
    root = Path(request["project_root"])
    params = deepcopy(request["parameters"])
    kind = request["kind"]

    def run():
        if kind == "report":
            from .models import Paper
            from .pipeline import DailyPipeline
            snapshot = checkpoint.get("paper") or params.get("snapshot")
            with DailyPipeline(config, root) as service:
                options = {"snapshot": Paper.from_dict(snapshot) if snapshot else None}
                if checkpoint.get("report_work"):
                    options["resume"] = checkpoint["report_work"]
                return service.report_arxiv_id(params["arxiv_id"], **options)
        if kind == "digest":
            from .pipeline import DailyPipeline
            with DailyPipeline(config, root) as service:
                return service.run(force=params["force"], demo=False, deliver=False)
        if kind == "weekly":
            from .weekly import WeeklySynthesizer
            with WeeklySynthesizer(config, root) as service:
                return service.generate(now=datetime.fromisoformat(params["now"]), include_notes=params["include_notes"])
        if kind == "compare":
            from .comparison import ComparisonService
            with ComparisonService(config, root) as service:
                return service.generate(deepcopy(params["snapshot"]))
        if kind == "version-batch":
            from .version_batch import VersionSyncBatch
            return VersionSyncBatch(config, root).run(params["batch_id"])
        raise ValueError("不支持的任务类型")

    return run
