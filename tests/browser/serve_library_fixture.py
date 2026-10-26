"""Disposable saved-paper library with reports, notes and every reading status."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import uvicorn

from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.utils import write_json
from arxiv_ra.web import create_app

directory = TemporaryDirectory(prefix='paperloom-library-preview-')
root = Path(directory.name)
config = root / 'config.yaml'
config.write_text('output_dir: run\ndiscovery:\n  interest_description: 智能体驱动的 SVG 生成与编辑\n', encoding='utf-8')
app = create_app(config)
active = ProfileManager(root).active_id()
store = PaperLibraryStore(root / 'run', active)
titles = [
    'EdiVal-Agent: An Object-Centric Framework for Automated, Fine-Grained Evaluation of Multi-Turn Editing',
    'SVGEditBench V2: A Benchmark for Instruction-based SVG Editing',
    'SVGEval: A Vision-Grounded Framework for Perceptual-Quality Benchmarking and Evaluation in Text-to-SVG Generation',
    'SVG-Score: Human-Aligned Evaluation of Text-to-SVG Generation',
    'VectorGym: A Multi-Task Benchmark for SVG Code Generation, Sketching and Editing',
]

def seed():
    store.state.path.unlink(missing_ok=True)
    for i, title in enumerate(titles):
        aid = f'2407.{5600 + i:05}'
        paper = {'arxiv_id': aid, 'version': 2, 'title': title, 'primary_category': 'cs.CV',
            'authors': [{'name': 'Ada Researcher'}, {'name': 'Yiming Wang'}, {'name': 'Kunato Nishina'}],
            'abs_url': f'https://arxiv.org/abs/{aid}', 'pdf_url': f'https://arxiv.org/pdf/{aid}',
            'abstract_zh': ('围绕矢量图像编辑中的复杂指令理解与精细控制，提出一个结合视觉反馈、工具调用与多轮修正的智能体评测框架。'
                '通过对目标对象、几何结构和渲染结果进行联合验证，评估模型在真实编辑任务中的表现。') * 3,
            'recommendation_detail': '1. 提供细粒度的图形编辑评测，覆盖目标定位、结构修改和视觉一致性。\n2. 与当前 SVG 生成及自主修正方向紧密相关，可用于后续实验设计与方法比较。',
            'conference_publications': [{'conference': 'neurips', 'year': 2025,
                'evidence_url': 'https://proceedings.neurips.cc/'}] if i == 1 else []}
        store.add({'paper': paper}, 'SVG Agents')
        status = ['unread', 'reading', 'read', 'paused', 'unread'][i]
        if status != 'unread':
            store.state.update_reading(aid, status=status, notes='关注细粒度编辑的评价指标，后续需要复现实验。\n原样记录 <script>example()</script>',
                tags=['待复现', 'SVG 评测'], read_version=1, expected_updated_at='')
        if i < 3:
            folder = root / 'run' / '2026-10-15' / 'reports' / f'paper-{i}'
            write_json(folder / 'metadata.json', {'profile_id': active, 'paper': paper})
            (folder / 'report.md').write_text('# Fixture report\n\nReading material.', encoding='utf-8')
            (folder / 'report.html').write_text('<h1>Fixture report</h1>', encoding='utf-8')
    state = store.state.snapshot()
    for i in range(len(titles)):
        state['library'][f'2407.{5600+i:05}']['saved_at'] = f'2026-10-{15-i:02}T12:00:00+00:00'
    write_json(store.state.path, state)


@app.post('/fixture/reset')
def reset_fixture():
    seed()
    return {'ok': True}

seed()
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8793, log_level='warning')
