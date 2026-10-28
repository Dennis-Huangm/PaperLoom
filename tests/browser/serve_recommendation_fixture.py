"""Isolated recommendation UI fixture; report submission is recorded, never executed."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import uvicorn
from arxiv_ra.papers.models import Paper
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app
from arxiv_ra.runtime.jobs.manager import JobManager, BackgroundJob

fixture = TemporaryDirectory(prefix="paperloom-recommendation-qa-")
root = Path(fixture.name)
config = root / "config.yaml"
config.write_text("output_dir: run\ndiscovery:\n  interest_description: preview\n", encoding="utf-8")
now = datetime.now(timezone.utc)
rows = []
for i, title in enumerate(["Visual Agents", "Reward Learning", "Legacy Paper"]):
    aid = f"2407.{5600 + i:05}"
    paper = Paper(aid, title, [], "Isolated browser acceptance fixture.", ["cs.AI"], "cs.AI",
                  now, now, f"https://arxiv.org/abs/{aid}", f"https://arxiv.org/pdf/{aid}")
    paper.version = 2 if i < 2 else None
    rows.append({"profile_id": "preview", "paper": paper.to_dict(), "verified": {}})
write_json(root / "run/2026-10-15/recommendations-preview.json", rows)

def submit(self, detail, request, *, identity):
    write_json(root / f"submitted-{request['parameters']['arxiv_id']}.json", request)
    return BackgroundJob(identity, "report", "测试报告", "queued", now.isoformat(), now.isoformat())

JobManager.submit_request = submit
print(f"RECOMMENDATION_FIXTURE {root}", flush=True)
app = create_app(config)

uvicorn.run(app, host="127.0.0.1", port=8773, log_level="warning")
