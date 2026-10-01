"""Serve isolated search data for search_smoke.cjs without external requests."""
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import uvicorn

from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.search import SearchIndex
from arxiv_ra.web import create_app

fixture_directory = TemporaryDirectory(prefix="paperloom-search-preview-")
root = Path(fixture_directory.name)
config_path = root / "config.yaml"
config_path.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
app = create_app(config_path)
helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / "test_search.py"))
helpers["report"](root / "run", "alpha")
store = PaperLibraryStore(root / "run", "alpha")
for i in range(26):
    aid = f"2407.{5600 + i:05}"
    store.add({"paper": {"arxiv_id": aid, "title": "Visual Agents: Learning to Reason with Feedback" if i == 0 else f"稀疏奖励与策略优化：研究记录 {i}", "abstract": "Reward calibration and policy optimization.", "version": 2}}, "Test")
    store.state.update_reading(aid, status="read", notes="奖励模型需要关注稀疏反馈、策略优化与复现实验。\nReward calibration and feedback support reliable reasoning.\n原样笔记 <script>bad()</script>", tags=["待复现"], read_version=2, expected_updated_at="")
SearchIndex(root / "run", "alpha").refresh()
print("SEARCH_PREVIEW_READY", flush=True)
uvicorn.run(app, host="127.0.0.1", port=8769, log_level="warning")
