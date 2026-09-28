import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import httpx
import uvicorn
from arxiv_ra.web import create_app
from arxiv_ra.config import load_config
from arxiv_ra.citation_graph import CitationExplorer
root = Path('work/graph-preview').resolve(); root.mkdir(parents=True, exist_ok=True)
config_path = root / 'config.yaml'
config_path.write_text('output_dir: run\ncitations:\n  max_references: 50\n  max_citations: 50\n  max_similar: 50\n  max_nodes: 50\n  max_candidates: 150\n  min_interval: 0\n  max_retries: 0\n', encoding='utf-8')
app = create_app(config_path)
cfg = load_config(config_path)
titles = ['Visual Planning with Language Models', 'Learning to Reason through Visual Feedback', 'World Models for Interactive Environments', 'Tool-Using Agents for Image Generation', 'Compositional Scene Understanding', 'Embodied Reasoning in Open Worlds', 'Evaluating Multimodal Planning Systems', 'Self-Refinement with Visual Critique', 'Retrieval-Augmented Visual Agents', 'Long-Horizon Planning with Memory', 'Cross-Domain Transfer in Medical Imaging', 'Physics-Informed Visual Simulation']
authors = ['Chen', 'Park', 'Li', 'Garcia', 'Huang', 'Kim', 'Singh', 'Wang', 'Smith', 'Zhao', 'Patel', 'Wu']
def paper(i):
 topic = i % 3
 abstract = ['Visual agents use language models to plan image generation with iterative feedback. This study evaluates multimodal planning and tool use across visual reasoning benchmarks.', 'World models learn physical simulation and embodied control. Interactive environments enable long-horizon planning with memory and compositional scene understanding.', 'Medical imaging uses cross-domain transfer to interpret anatomical structures. Clinical evaluation explores reliable visual diagnosis through evidence and feedback.'][topic]
 return {'paperId': f'P{i:03}', 'title': titles[i % len(titles)] + (f': Study {i // 12 + 1}' if i >= 12 else ''), 'abstract': abstract, 'year': 2015 + i % 12, 'citationCount': (i * 71) % 1400, 'authors': [{'name': 'Alex ' + authors[i % 12]}], 'venue': ['ICLR', 'NeurIPS', 'ICML'][topic], 'externalIds': {'ArXiv': f'2407.{i+10000}'}, 'url': f'https://www.semanticscholar.org/paper/P{i:03}'}
def handle(request):
 if '/paper/ARXIV:' in request.url.path:
  return httpx.Response(200,json={**paper(0), 'title': 'Visual Agents: Planning, Reasoning and Learning from Feedback'})
 if request.method == 'POST':
  ids=json.loads(request.content)['ids'];return httpx.Response(200,json=[{'paperId': pid,'references':[{'paperId':f'P{(int(pid[1:])+3)%150:03}', 'title':'Related visual planning work'}, {'paperId':'SHARED','title':'Attention Is All You Need'}]} for pid in ids])
 if '/forpaper/' in request.url.path: return httpx.Response(200,json={'recommendedPapers':[paper(i) for i in range(101,150)]})
 key='citedPaper' if request.url.path.endswith('references') else 'citingPaper'
 start=1 if key=='citedPaper' else 51
 return httpx.Response(200,json={'data':[{key:paper(i)} for i in range(start,start+50)]})
for size in (50,150):
 cfg.citations.max_nodes=size
 with CitationExplorer(cfg, root) as exp:
  exp.client.close();exp.client=httpx.Client(transport=httpx.MockTransport(handle))
  path=exp.generate('2407.05600' if size==50 else '2407.05601')
  (root / f'path-{size}.txt').write_text(str(path),encoding='utf-8')
print('PREVIEW_READY', flush=True)
uvicorn.run(app, host='127.0.0.1', port=8768, log_level='warning')
