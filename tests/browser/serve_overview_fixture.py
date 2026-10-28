"""Isolated overview preview with deterministic external research adapters."""
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import uvicorn

from paperloom.web import create_app
from paperloom.runtime.research_clients import ResearchClients
from paperloom.profiles.direction_overview import OverviewGenerator
from paperloom.config import load_config
from tests.profiles.test_direction_overview import direction, LiteratureAPI, OverviewModel


class Model(OverviewModel):
    def chat(self, system, prompt, **kwargs):
        if kwargs.get("json_mode"):
            time.sleep(1.5)
            return super().chat(system, prompt, **kwargs)
        return "\n\n".join([
            "## 研究对象与问题边界\n\nSVG 生成与编辑研究如何用模型构建可编辑的矢量图形。与直接生成像素图像不同，这类方法需要组织路径、控制点与图层结构 [1]。",
            "## 背景与关键概念\n\n矢量图通过几何路径表达形状。理解路径、控制点和渲染过程，有助于阅读不同路线的论文。",
            "## 主要技术路线\n\n一类路线直接预测结构化表示，另一类路线利用图像反馈优化图形。比较它们时，需要分别考虑视觉质量与编辑能力 [2]。",
            "## 代表工作与近期进展\n\n下面的文献用于说明方法思路。这些材料只覆盖本次取得的候选，尚不足以代表方向的全部进展 [3]。",
            "## 当前局限与入门阅读顺序\n\n建议先理解图形表示，再阅读早期优化方法，最后比较近期模型。复杂结构的稳定生成仍需要更多材料支持。",
        ])


ResearchClients.llm = property(lambda self: Model())
ResearchClients.arxiv = property(lambda self: LiteratureAPI())
ResearchClients.conferences = property(lambda self: SimpleNamespace(discover=lambda *a, **kw: ([], {})))
root = Path(tempfile.mkdtemp(prefix="paperloom-overview-preview-"))
config = root / "config.yaml"
config.write_text("output_dir: run\ndiscovery:\n  interest_description: SVG editing\n", encoding="utf-8")
app = create_app(config)
profile = direction()
app.state.profiles.save(profile)
with OverviewGenerator(load_config(config), root) as generator:
    generator.generate(profile)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8776, log_level="warning")
