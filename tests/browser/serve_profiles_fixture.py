"""Isolated direction library preview. Uses local fixture data only."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import uvicorn

from arxiv_ra.config import AppConfig
from arxiv_ra.profiles.profile_plan import new_draft
from arxiv_ra.profiles.profiles import ProfileManager
from arxiv_ra.web import create_app

directory = TemporaryDirectory(prefix='paperloom-profiles-preview-')
root = Path(directory.name)
config = root / 'config.yaml'
config.write_text('output_dir: run\n', encoding='utf-8')
app = create_app(config)
manager = ProfileManager(root)
initial_profile = manager.active_id()
description = ('聚焦基于可缩放矢量图形（SVG）的代码原生 Agent 文本生成图像技术，涵盖 SVG 的生成、结构化编辑、多模态理解与精确修复。'
    '重点关注引入工具调用、渲染反馈闭环与生成—自省—修正机制的自主优化方法。'
    '同时高度关注 SVG 生成、编辑与动画相关的基准评测数据集与自动化评测框架构建。')
for key, name, desc in [
    (initial_profile, '智能体驱动的 SVG 生成与编辑', description),
    ('image-agents', '多模态智能体图像生成与编辑', '研究由 LLM/VLM/多模态 Agent 驱动的图像生成与编辑，重点关注任务分解、规划、工具选择与调用、结果验证、自我修正和迭代反馈。'),
]:
    manager.save({'id': key, 'name': name, 'description': desc,
        'discovery': {'interest_description': desc, 'positive_keywords': ['agent', 'agentic t2i', 'text to image', 'svg', 'benchmark', 'evaluation', 'visual feedback'],
            'lookback_days': 365, 'recommendation_count': 20},
        'source': {'reference_papers': [{'arxiv_id': f'2407.{5600 + i:05}',
            'title': ['Vector-Bench: Can Models Surgically Edit SVG Code?', 'Render-in-the-Loop: Vector Graphics Generation via Visual Self-Feedback',
                'IntroSVG: Learning from Rendering Feedback for Text-to-SVG Generation', 'MoVer: Motion Verification for Motion Graphics Animation'][i % 4]} for i in range(6)]}})

@app.post('/fixture/reset')
def reset_fixture():
    manager.activate(initial_profile)
    for folder in ('drafts', 'deleted-drafts'):
        for key in ('world-models', 'failed-draft'):
            (root / 'profiles' / folder / f'{key}.yaml').unlink(missing_ok=True)
    return {'ok': True}


# Add draft data through the local fixture endpoint; default is an empty list.
@app.post('/fixture/drafts')
def add_drafts():
    for key, name, failed in [('world-models', '具身智能中的世界模型', False), ('failed-draft', '多模态推理与工具调用', True)]:
        draft = new_draft(AppConfig(), key, name, ['world model'], [], [], '围绕机器人控制与长期规划整理研究方向。',
            {'interest_description': 'World models', 'arxiv_query_terms': ['world model']},
            error='模型连接失败，请稍后重新生成。' if failed else '')
        manager.save_draft(draft)
    return {'ok': True}

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8792, log_level='warning')
