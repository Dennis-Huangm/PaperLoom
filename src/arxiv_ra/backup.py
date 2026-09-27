"""Local portable backups and previewed restores into managed, isolated folders."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid
import zipfile
import zlib

import yaml

from .config import load_config
from .job_store import JobStore
from .reading_state import _locked
from .utils import write_json, read_json


MAX_FILES = 100000
MAX_TOTAL = 20 * 1024**3
MAX_FILE = 2 * 1024**3
DATA_DIRS = {"papers", "version-batches", "versions", "weekly", "comparisons", "citations", "metadata-cache"}
EXTENSIONS = {".json", ".md", ".html", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg"}


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_name(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute() or str(path) != value
            or any(part in {".", ".."} or part.endswith((".", " "))
                   or re.search(r'[<>:"|?*\x00-\x1f]', part)
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                   for part in path.parts)):
        raise ValueError("备份包含不安全的文件路径")
    return path


def allowed(value: str) -> bool:
    parts = safe_name(value).parts
    if value in {"project/config.yaml", "project/.env", "project/profiles/active.txt"}:
        return True
    if len(parts) == 3 and parts[:2] == ("project", "profiles"):
        return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.yaml", parts[2]))
    if parts[0] != "data" or len(parts) < 2:
        return False
    data = parts[1:]
    if any(part.startswith(".") for part in data) and not (len(data) == 2 and data[0] == ".jobs"):
        return False
    if len(data) == 1:
        return Path(data[0]).suffix.lower() == ".json"
    if data[0] == ".jobs":
        return bool(re.fullmatch(r"[a-f0-9]+\.json", data[1]))
    return (data[0] in DATA_DIRS or bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", data[0]))) and Path(data[-1]).suffix.lower() in EXTENSIONS


def contained(path: Path, root: Path) -> Path:
    path.resolve().relative_to(root.resolve())
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("备份和恢复不接受符号链接或目录联接")
    return path


def inventory(root: Path) -> dict[str, Path]:
    result = {}
    if not root.exists():
        return result
    contained(root, root)
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            contained(Path(directory) / name, root)
        for name in files:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = path
    return result


def tree_hash(root: Path) -> str:
    records = [(name, sha(path)) for name, path in sorted(inventory(root).items())
               if not name.endswith(".lock")]
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def rename_directory(source: Path, target: Path) -> None:
    for attempt in range(8):
        try:
            source.rename(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                raise
            time.sleep(min(.02 * 2**attempt, .2))


class BackupService:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.project = self.config_path.parent
        config = load_config(self.config_path)
        output = Path(config.output_dir)
        self.output = (output if output.is_absolute() else self.project / output).resolve()
        self.backups = contained(self.project / "backups", self.project)
        self.restores = contained(self.project / "restored", self.project)

    def _selection(self, reports: bool, pdfs: bool, secrets: bool) -> dict[str, Path]:
        files = {"project/config.yaml": self.config_path}
        if secrets and (self.project / ".env").is_file():
            files["project/.env"] = contained(self.project / ".env", self.project)
        for name, path in inventory(self.project / "profiles").items():
            key = "project/profiles/" + name
            if allowed(key):
                files[key] = path
        for name, path in inventory(self.output).items():
            key = "data/" + name
            if not allowed(key):
                continue
            parts = PurePosixPath(name).parts
            artifact = ("reports" in parts or parts[0] in {"weekly", "comparisons", "citations", "versions"}
                        or path.suffix.lower() not in {".json", ".pdf"})
            if path.suffix.lower() == ".pdf":
                if not pdfs:
                    continue
            elif artifact and not reports and not (pdfs and path.name == "metadata.json" and path.with_name("paper.pdf").is_file()):
                continue
            files[key] = path
        return files

    def create(self, *, reports=False, pdfs=False, secrets=False) -> Path:
        self.backups.mkdir(parents=True, exist_ok=True)
        name = f"paperloom-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
        destination = self.backups / name
        temporary = destination.with_suffix(".tmp")
        files = self._selection(reports, pdfs, secrets)
        before = {key: (p.stat().st_size, p.stat().st_mtime_ns) for key, p in files.items()}
        if len(files) > MAX_FILES or sum(item[0] for item in before.values()) > MAX_TOTAL:
            raise ValueError("备份超出首版限制（10 万文件 / 20 GiB）")
        entries = []
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for key, path in sorted(files.items()):
                    if before[key][0] > MAX_FILE:
                        raise ValueError("单文件不能超过 2 GiB")
                    digest = hashlib.sha256()
                    with path.open("rb") as source, archive.open(key, "w", force_zip64=True) as target:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(block)
                            digest.update(block)
                    entries.append({"path": key, "size": before[key][0], "sha256": digest.hexdigest()})
                manifest = {"format": "paperloom-backup", "version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                            "source_project": str(self.project), "source_output": str(self.output),
                            "options": {"reports": reports, "pdfs": pdfs, "secrets": secrets}, "files": entries}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            after = self._selection(reports, pdfs, secrets)
            if before != {key: (p.stat().st_size, p.stat().st_mtime_ns) for key, p in after.items()}:
                raise ValueError("资料在备份期间发生变化，请等待任务结束并停止编辑后重试")
            self.inspect(temporary)
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def inspect(path: Path) -> dict:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES + 1 or sum(item.file_size for item in infos) > MAX_TOTAL:
                    raise ValueError("备份大小或文件数超出限制")
                names = [item.filename for item in infos]
                if len({name.casefold() for name in names}) != len(names):
                    raise ValueError("备份包含重复或大小写冲突路径")
                info = archive.getinfo("manifest.json")
                if info.file_size > 16 * 1024**2:
                    raise ValueError("备份清单过大")
                manifest = json.loads(archive.read(info))
                if manifest.get("format") != "paperloom-backup" or manifest.get("version") != 1:
                    raise ValueError("不支持的备份格式或版本")
                if any(not isinstance(manifest.get(key), str) for key in ("created_at", "source_project", "source_output")):
                    raise ValueError("备份来源或时间记录无效")
                records = manifest["files"]
                if len({item["path"] for item in records}) != len(records) or set(names) != {"manifest.json", *(item["path"] for item in records)}:
                    raise ValueError("备份文件与清单不一致")
                if "project/config.yaml" not in names:
                    raise ValueError("备份缺少配置文件")
                for record in records:
                    name = record["path"]
                    if not allowed(name):
                        raise ValueError("备份包含不支持的路径")
                    info = archive.getinfo(name)
                    mode = info.external_attr >> 16
                    if info.is_dir() or stat.S_ISLNK(mode) or info.flag_bits & 1 or info.file_size > MAX_FILE:
                        raise ValueError("备份包含链接、加密文件或过大文件")
                    if type(record["size"]) is not int or info.file_size != record["size"]:
                        raise ValueError("备份文件长度不匹配")
                    digest = hashlib.sha256()
                    with archive.open(info) as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(block)
                    if digest.hexdigest() != record["sha256"]:
                        raise ValueError("备份完整性校验失败")
                manifest["has_secrets"] = "project/.env" in names
                return manifest
        except (zipfile.BadZipFile, zlib.error, KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份损坏或清单无效") from exc

    def target(self, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", name):
            raise ValueError("恢复目录名称仅支持 1–48 位字母、数字、短横线和下划线")
        safe_name(name)
        target = contained(self.restores / name, self.project)
        if target == self.output or target in self.output.parents or self.output in target.parents:
            raise ValueError("恢复目录不能与当前输出目录重叠")
        if target.exists() and not (target / ".paperloom-restore.json").is_file():
            raise ValueError("已有目录不是本功能创建的恢复目录，请换一个名称")
        return target

    @staticmethod
    def _mapped(name: str) -> str:
        return name.removeprefix("project/") if name.startswith("project/") else "run/" + name.removeprefix("data/")

    def _bytes(self, archive, record, manifest, target):
        name = record["path"]
        # Most files (especially PDFs) are streamed separately without loading into memory.
        raw = archive.read(name)
        if name == "project/config.yaml":
            config = yaml.safe_load(raw)
            if not isinstance(config, dict):
                raise ValueError("备份配置格式错误")
            config["output_dir"] = "run"
            return yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
        payload = json.loads(raw)
        def rewrite(value):
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {}
            for key, item in value.items():
                if key in {"project_root", "output_dir", "report_path", "pdf_path"} and isinstance(item, str):
                    normalized = item.replace("\\", "/")
                    for source, destination in ((manifest.get("source_output"), target / "run"), (manifest.get("source_project"), target)):
                        prefix = str(source or "").replace("\\", "/").rstrip("/")
                        if prefix and (normalized.casefold() == prefix.casefold() or normalized.casefold().startswith(prefix.casefold() + "/")):
                            item = str(destination) + normalized[len(prefix):]
                            break
                    if key == "project_root":
                        item = str(target)
                    if key == "output_dir":
                        item = str(target / "run")
                result[key] = rewrite(item)
            return result
        return json.dumps(rewrite(payload), ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _transform(name):
        return name == "project/config.yaml" or (name.startswith(("data/.jobs/", "data/version-batches/", "data/version-state-")) and name.endswith(".json"))

    def preview(self, archive_path: Path, name: str, mode="keep") -> dict:
        if mode not in {"keep", "replace"}:
            raise ValueError("无效的冲突处理方式")
        target = self.target(name)
        manifest = self.inspect(archive_path)
        files = []
        with zipfile.ZipFile(archive_path) as archive:
            for record in manifest["files"]:
                mapped = self._mapped(record["path"])
                path = contained(target / mapped, target)
                fingerprint = record["sha256"]
                if self._transform(record["path"]):
                    if record["size"] > 64 * 1024**2:
                        raise ValueError("需要迁移的配置或任务记录过大")
                    fingerprint = hashlib.sha256(self._bytes(archive, record, manifest, target)).hexdigest()
                existing = sha(path) if path.is_file() else None
                if path.exists() and not path.is_file():
                    raise ValueError("恢复文件与已有目录冲突")
                action = "新增" if existing is None else "相同" if existing == fingerprint else "保留" if mode == "keep" else "替换"
                files.append({"path": mapped, "action": action})
        archive_hash = sha(archive_path)
        current_hash = tree_hash(target)
        token = hashlib.sha256(json.dumps([archive_hash, str(target), current_hash, mode]).encode()).hexdigest()
        return {"archive": str(archive_path.resolve()), "name": name, "target": str(target), "mode": mode,
                "token": token, "archive_hash": archive_hash, "tree_hash": current_hash, "files": files,
                "created_at": manifest["created_at"], "has_secrets": manifest["has_secrets"],
                "counts": {action: sum(item["action"] == action for item in files) for action in ("新增", "相同", "保留", "替换")}}

    def restore(self, archive_path: Path, name: str, *, token: str, mode="keep") -> dict:
        self.restores.mkdir(parents=True, exist_ok=True)
        target = self.target(name)
        with _locked(self.restores / f".{name}.lock"), closing(JobStore(self.restores / ".owners" / name, guard_restore=False)):
            if self.pending(name):
                raise ValueError("该目录有中断的恢复操作，请先整理中断恢复，再重新预览")
            plan = self.preview(archive_path, name, mode)
            if plan["token"] != token:
                raise ValueError("备份或目标目录已变化，请重新预览后再恢复")
            if target.exists():
                owner = JobStore(target / "run", guard_restore=False)
                owner.close()
            identity = uuid.uuid4().hex
            stage = self.restores / f".stage-{identity}"
            before = self.restores / f"{name}-before-{identity[:12]}"
            journal = self.restores / f".restore-{identity}.json"
            manifest = self.inspect(archive_path)
            try:
                if target.exists():
                    shutil.copytree(target, stage)
                else:
                    stage.mkdir()
                actions = {item["path"]: item["action"] for item in plan["files"]}
                with zipfile.ZipFile(archive_path) as archive:
                    for record in manifest["files"]:
                        mapped = self._mapped(record["path"])
                        if actions[mapped] in {"相同", "保留"}:
                            continue
                        destination = stage / mapped
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if self._transform(record["path"]):
                            destination.write_bytes(self._bytes(archive, record, manifest, target))
                        else:
                            with archive.open(record["path"]) as source, destination.open("wb") as output:
                                shutil.copyfileobj(source, output, 1024 * 1024)
                if str(load_config(stage / "config.yaml").output_dir) != "run":
                    raise ValueError("恢复配置必须使用独立 run 目录；请选择替换冲突配置")
                if sha(archive_path) != plan["archive_hash"] or tree_hash(target) != plan["tree_hash"]:
                    raise ValueError("资料在恢复期间发生变化，请重新预览")
                write_json(stage / ".paperloom-restore.json", {"version": 1, "archive_sha256": plan["archive_hash"]})
                write_json(journal, {"status": "prepared", "target": str(target), "before": str(before), "stage": str(stage)})
                if target.exists():
                    rename_directory(target, before)
                try:
                    rename_directory(stage, target)
                except BaseException:
                    if before.exists() and not target.exists():
                        rename_directory(before, target)
                    raise
                write_json(journal, {"status": "complete", "target": str(target), "before": str(before) if before.exists() else ""})
                return {"target": str(target), "before": str(before) if before.exists() else "", "counts": plan["counts"]}
            finally:
                if stage.exists():
                    # This is our own randomly named staging directory, never the target.
                    contained(stage, self.restores)
                    shutil.rmtree(stage)

    def list(self) -> list[dict]:
        return [{"name": path.name, "path": str(path), "size": path.stat().st_size}
                for path in sorted(self.backups.glob("paperloom-*.zip"), reverse=True) if path.is_file()]

    def pending(self, name: str | None = None) -> list[dict]:
        result = []
        for path in self.restores.glob(".restore-*.json"):
            value = read_json(path)
            if value.get("status") == "prepared" and (name is None or Path(value["target"]).name == name):
                result.append({**value, "journal": str(path)})
        return result

    def recover_interrupted(self) -> int:
        count = 0
        for value in self.pending():
            target = Path(value["target"])
            name = target.name
            self.target(name)
            before = Path(value["before"])
            if (target.parent != self.restores or before.parent != self.restores
                    or not re.fullmatch(re.escape(name) + r"-before-[a-f0-9]{12}", before.name)):
                raise ValueError("恢复日志路径无效，请检查本地恢复记录")
            with _locked(self.restores / f".{name}.lock"), closing(JobStore(self.restores / ".owners" / name, guard_restore=False)):
                contained(before, self.restores)
                if not target.exists() and before.exists():
                    rename_directory(before, target)
                # A published target is kept. Unpublished staging data is retained
                # for inspection; no user data is deleted during crash recovery.
                write_json(Path(value["journal"]), {**value, "status": "reconciled"})
                count += 1
        return count
