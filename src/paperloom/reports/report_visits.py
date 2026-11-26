"""Last successful report opens, shared by all versions and directions of a paper."""
from datetime import datetime, timezone
import logging
from pathlib import Path

from ..papers.paper_data import base_id
from ..papers.reading_state import _locked
from ..utils import read_json, write_json


logger = logging.getLogger(__name__)


def report_visit_key(arxiv_id: str, report_id: str) -> str:
    identity = base_id(arxiv_id)
    return f"paper:{identity}" if identity else f"report:{report_id}"


class ReportVisitStore:
    def __init__(self, output_root: Path) -> None:
        self.path = output_root / ".report-visits" / "history.json"
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self) -> dict[str, str]:
        visits = read_json(self.path, {})
        if not isinstance(visits, dict):
            raise ValueError("报告打开记录格式错误")
        return visits

    def snapshot(self) -> dict[str, str]:
        # Atomic replacement makes an unlocked read a complete snapshot.
        try:
            return self._read()
        except (OSError, ValueError):
            logger.warning("无法读取报告打开记录：%s", self.path, exc_info=True)
            return {}

    def record(self, arxiv_id: str, report_id: str) -> None:
        with _locked(self.lock_path):
            visits = self._read()
            visits[report_visit_key(arxiv_id, report_id)] = datetime.now(timezone.utc).isoformat()
            write_json(self.path, visits)
