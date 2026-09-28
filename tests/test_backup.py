import hashlib
import json
import stat
from pathlib import Path
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
import yaml

from arxiv_ra.backup import BackupService, sha
from arxiv_ra.cli import main
from arxiv_ra.job_store import JobStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.utils import write_json, read_json
from arxiv_ra.web import create_app


@pytest.fixture
def service(tmp_path):
    config = tmp_path / "custom-config.yaml"
    config.write_text("output_dir: data\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_API_KEY=secret-fixture", encoding="utf-8")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "active.txt").write_text("alpha")
    (profiles / "alpha.yaml").write_text("id: alpha\nname: Alpha\n", encoding="utf-8")
    store = PaperLibraryStore(tmp_path / "data", "alpha")
    store.add({"paper": {"arxiv_id": "2407.05600", "version": 2, "title": "Fixture"}}, "Alpha")
    store.state.update_reading("2407.05600", status="read", notes="个人结论", tags=["标签"],
                              read_version=1, expected_updated_at="")
    report = tmp_path / "data/2026-09-25/reports/example/report.md"
    report.parent.mkdir(parents=True)
    report.write_text("report fixture", encoding="utf-8")
    report.with_name("paper.pdf").write_bytes(b"pdf fixture")
    write_json(report.with_name("metadata.json"), {"paper": {"arxiv_id": "2407.05600", "version": 2}, "profile_id": "alpha"})
    write_json(tmp_path / "data/.jobs/abcdef012345.json", {"version": 1, "job": {"id": "abcdef012345"},
        "request": {"project_root": str(tmp_path), "config": {"output_dir": str(tmp_path / "data")}}})
    write_json(tmp_path / "data/.search/private.json", {"text": "search cache"})
    return BackupService(config)


def test_default_scope_and_full_backup_restore_with_path_migration(service, monkeypatch):
    monkeypatch.setenv("RUNTIME_ONLY_SECRET", "not-in-backup")
    basic = service.create()
    manifest = service.inspect(basic)
    names = {item["path"] for item in manifest["files"]}
    assert "project/.env" not in names and not manifest["has_secrets"]
    assert "data/reading-state-alpha.json" in names and "data/.jobs/abcdef012345.json" in names
    assert not any(".search" in name or name.endswith((".md", ".pdf")) for name in names)
    with zipfile.ZipFile(basic) as archive:
        assert all(b"not-in-backup" not in archive.read(name) for name in archive.namelist())
    pdf_backup = service.create(pdfs=True)
    pdf_names = {item["path"] for item in service.inspect(pdf_backup)["files"]}
    assert "data/2026-09-25/reports/example/metadata.json" in pdf_names
    assert "data/2026-09-25/reports/example/report.md" not in pdf_names
    archive = service.create(reports=True, pdfs=True, secrets=True)
    before = sha(service.output / "reading-state-alpha.json")
    plan = service.preview(archive, "copy")
    assert plan["has_secrets"] and plan["counts"]["新增"] > 0
    result = service.restore(archive, "copy", token=plan["token"])
    root = Path(result["target"])
    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))["output_dir"] == "run"
    assert (root / ".env").read_text() == "LLM_API_KEY=secret-fixture"
    assert "not-in-backup" not in str(service.inspect(archive))
    assert (root / "run/2026-09-25/reports/example/paper.pdf").read_bytes() == b"pdf fixture"
    state = read_json(root / "run/reading-state-alpha.json")
    assert state["reading"]["2407.05600"]["notes"] == "个人结论"
    task = read_json(root / "run/.jobs/abcdef012345.json")
    assert task["request"]["project_root"] == str(root)
    assert task["request"]["config"]["output_dir"] == str(root / "run")
    assert sha(service.output / "reading-state-alpha.json") == before


def test_conflict_keep_replace_and_stale_preview(service):
    archive = service.create()
    plan = service.preview(archive, "copy")
    root = Path(service.restore(archive, "copy", token=plan["token"])["target"])
    state = root / "run/reading-state-alpha.json"
    state.write_text('{"modified":true}', encoding="utf-8")
    (root / "extra.txt").write_text("keep unrelated")
    plan = service.preview(archive, "copy")
    assert plan["counts"]["保留"] == 1
    result = service.restore(archive, "copy", token=plan["token"])
    assert read_json(state) == {"modified": True} and Path(result["before"]).is_dir()
    plan = service.preview(archive, "copy", "replace")
    (root / "extra.txt").write_text("changed after preview")
    with pytest.raises(ValueError, match="已变化"):
        service.restore(archive, "copy", token=plan["token"], mode="replace")
    plan = service.preview(archive, "copy", "replace")
    result = service.restore(archive, "copy", token=plan["token"], mode="replace")
    assert read_json(state)["reading"]
    assert read_json(Path(result["before"]) / "run/reading-state-alpha.json") == {"modified": True}
    assert (root / "extra.txt").read_text() == "changed after preview"


def rewrite_zip(source, destination, mutate):
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    mutate(contents)
    with zipfile.ZipFile(destination, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)


@pytest.mark.parametrize("name", ["data/../escape.json", "data/C:/escape.json", "data/2026-09-25/CON.json", "data/2026-09-25/a.json:stream", "project/evil.py", "data/.search/secret.json"])
def test_unsafe_archives_are_rejected(service, name):
    source = service.create()
    malicious = service.project / "bad.zip"
    def mutate(contents):
        body = b"{}"
        manifest = json.loads(contents["manifest.json"])
        manifest["files"].append({"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        contents[name] = body
        contents["manifest.json"] = json.dumps(manifest).encode()
    rewrite_zip(source, malicious, mutate)
    with pytest.raises(ValueError):
        service.preview(malicious, "copy")
    assert not service.restores.exists()


def test_corruption_and_archive_changed_after_preview(service):
    archive = service.create()
    plan = service.preview(archive, "copy")
    changed = service.project / "changed.zip"
    rewrite_zip(archive, changed, lambda contents: contents.update({"project/config.yaml": b"invalid"}))
    with pytest.raises(ValueError):
        service.inspect(changed)
    changed.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        service.inspect(changed)
    other = service.create(secrets=True)
    with pytest.raises(ValueError, match="已变化"):
        service.restore(other, "copy", token=plan["token"])


def test_publication_failure_rolls_back_and_crash_gap_is_reconciled(service):
    archive = service.create()
    plan = service.preview(archive, "copy")
    root = Path(service.restore(archive, "copy", token=plan["token"])["target"])
    original = sha(root / "config.yaml")
    plan = service.preview(archive, "copy", "replace")
    rename = Path.rename
    def fail_stage(self, target):
        if self.name.startswith(".stage-"):
            raise OSError("simulated publication failure")
        return rename(self, target)
    with patch.object(Path, "rename", fail_stage), pytest.raises(OSError):
        service.restore(archive, "copy", token=plan["token"], mode="replace")
    assert root.exists() and sha(root / "config.yaml") == original
    assert service.recover_interrupted() == 1
    before = service.restores / "copy-before-abcdef012345"
    root.rename(before)
    write_json(service.restores / ".restore-crash.json", {"status": "prepared", "target": str(root), "before": str(before), "stage": "unused"})
    assert service.recover_interrupted() == 1 and root.exists()
    assert sha(root / "config.yaml") == original


def test_running_target_and_unmanaged_directories_rejected(service):
    service.restores.mkdir()
    (service.restores / "unmanaged").mkdir()
    with pytest.raises(ValueError, match="不是本功能"):
        service.preview(service.create(), "unmanaged")
    archive = service.create()
    plan = service.preview(archive, "copy")
    root = Path(service.restore(archive, "copy", token=plan["token"])["target"])
    plan = service.preview(archive, "copy")
    owner = JobStore(root / "run")
    try:
        with pytest.raises(RuntimeError, match="GUI"):
            service.restore(archive, "copy", token=plan["token"])
    finally:
        owner.close()


def test_changed_source_aborts_backup_without_publishing(service):
    select = service._selection
    def changing(*args):
        result = select(*args)
        if changing.calls:
            service.config_path.write_text("output_dir: other\n", encoding="utf-8")
        changing.calls += 1
        return result
    changing.calls = 0
    with patch.object(service, "_selection", side_effect=changing), pytest.raises(ValueError, match="发生变化"):
        service.create()
    assert not service.list()


def test_web_preview_restore_and_cli(service, capsys):
    app = create_app(service.config_path)
    with TestClient(app) as client:
        assert "备份与恢复" in client.get("/settings").text
        assert client.get("/backups").headers["cache-control"] == "no-store"
        response = client.post("/backups/create", data={"profile_id": "alpha"})
        assert response.status_code == 200
        archive = Path(service.list()[0]["path"])
        assert client.get(f"/backups/download/{archive.name}").status_code == 200
        assert client.post("/backups/create", data={"profile_id": "wrong"}).status_code == 409
        plan = service.preview(archive, "copy")
        data = {"profile_id": "alpha", "archive_path": str(archive), "name": "copy"}
        preview = client.post("/backups/preview", data=data)
        assert preview.status_code == 200 and "确认恢复范围" in preview.text
        response = client.post("/backups/restore", data={**data, "token": plan["token"]})
        assert response.status_code == 200 and "恢复完成" in response.text
    main(["--config", str(service.config_path), "backup", "preview", str(archive), "--name", "cli-copy"])
    result = json.loads(capsys.readouterr().out)
    main(["--config", str(service.config_path), "backup", "restore", str(archive), "--name", "cli-copy", "--token", result["token"]])
    assert json.loads(capsys.readouterr().out)["target"].endswith("cli-copy")


def test_zip_symlinks_duplicates_and_declared_size_limit(service):
    source = service.create()
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    bad = service.project / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name)
            if name == "project/config.yaml":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    with pytest.raises(ValueError, match="链接"):
        service.inspect(bad)
    with zipfile.ZipFile(bad, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr("PROJECT/CONFIG.YAML", contents["project/config.yaml"])
    with pytest.raises(ValueError, match="大小写"):
        service.inspect(bad)
    with patch("arxiv_ra.backup.MAX_TOTAL", 1), pytest.raises(ValueError, match="超出限制"):
        service.inspect(source)


def test_restore_guard_blocks_gui_start_during_publication(service):
    archive = service.create()
    plan = service.preview(archive, "copy")
    root = Path(service.restore(archive, "copy", token=plan["token"])["target"])
    guard = JobStore(service.restores / ".owners/copy", guard_restore=False)
    try:
        with pytest.raises(RuntimeError):
            JobStore(root / "run")
    finally:
        guard.close()
    owner = JobStore(root / "run")
    owner.close()


def test_graph_publication_keeps_local_assets_after_restore(service):
    snapshot = service.output / 'citations' / '2407.05600-alpha' / 'snapshots' / ('a' * 32)
    snapshot.mkdir(parents=True)
    (snapshot / 'index.html').write_text('<script src="graph.js"></script><link rel="stylesheet" href="graph.css">')
    (snapshot / 'graph.js').write_text('document.body.dataset.ready="true";')
    (snapshot / 'graph.css').write_text('body { color: #283333; }')
    write_json(snapshot / 'graph.json', {'schema_version': 2, 'seed': {'paperId': 'S'}})
    write_json(snapshot.parent.parent / 'active.json', {'snapshot_id': 'a' * 32})
    archive = service.create(reports=True)
    plan = service.preview(archive, 'graph-copy')
    result = service.restore(archive, 'graph-copy', token=plan['token'])
    restored = Path(result['target']) / 'run' / snapshot.relative_to(service.output)
    assert (restored / 'graph.js').read_text() == (snapshot / 'graph.js').read_text()
    assert (restored / 'graph.css').read_text() == (snapshot / 'graph.css').read_text()
