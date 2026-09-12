from pathlib import Path

from arxiv_ra.utils import read_json, write_json


def test_write_json_replaces_atomically_without_temporary_files(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    write_json(destination, {"version": 1})
    write_json(destination, {"version": 2, "items": [1, 2, 3]})

    assert read_json(destination) == {"version": 2, "items": [1, 2, 3]}
    assert list(tmp_path.glob(".*.tmp")) == []
