from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from arxiv_ra.utils import read_json, write_json


def test_write_json_replaces_atomically_without_temporary_files(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    write_json(destination, {"version": 1})
    write_json(destination, {"version": 2, "items": [1, 2, 3]})

    assert read_json(destination) == {"version": 2, "items": [1, 2, 3]}
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows read handles block replacement")
def test_atomic_write_survives_a_reader_temporarily_holding_destination(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    write_json(destination, {"version": 1})
    denied = threading.Event()
    replace = Path.replace

    def observed_replace(source, target):
        try:
            return replace(source, target)
        except PermissionError:
            denied.set()
            raise

    monkeypatch.setattr(Path, "replace", observed_replace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with destination.open("rb") as reader:
            future = pool.submit(write_json, destination, {"version": 2})
            assert denied.wait(3), "Reader should block at least one real Windows rename"
            assert b'"version": 1' in reader.read()
        future.result(timeout=3)
    assert read_json(destination) == {"version": 2}
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_persistent_denial_is_bounded_and_keeps_original(tmp_path, monkeypatch):
    import arxiv_ra.utils as utils
    destination = tmp_path / "state.json"
    write_json(destination, {"version": 1})
    attempts = []

    def denied_replace(*args):
        attempts.append(1)
        error = PermissionError(13, "destination remains locked")
        error.winerror = 5
        raise error

    monkeypatch.setattr(Path, "replace", denied_replace)
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        write_json(destination, {"version": 2})
    assert 1 < len(attempts) <= 8
    assert read_json(destination) == {"version": 1}
    assert not list(tmp_path.glob(".*.tmp"))
