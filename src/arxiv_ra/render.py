from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

from .models import Paper, ReportArtifact, VerifiedMetadata


STYLE = """
body{font-family:Inter,"Noto Sans SC",system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:32px;color:#172033;background:#f5f7fb}
main,article{background:#fff;border:1px solid #e4e8f0;border-radius:14px;padding:28px;margin-bottom:20px;box-shadow:0 8px 30px rgba(20,30,55,.06)}
a{color:#3b5ccc;text-decoration:none} h1,h2{line-height:1.25} .paper{display:grid;grid-template-columns:1fr auto;gap:18px;align-items:start}
.score{background:#edf2ff;color:#2948a5;border-radius:999px;padding:7px 12px;font-weight:700}.meta{color:#657087;font-size:.94rem}
img{max-width:100%;height:auto;border-radius:8px} table{border-collapse:collapse;width:100%} td,th{border:1px solid #dfe4ec;padding:8px;text-align:left}
code{background:#eff2f7;padding:2px 5px;border-radius:4px} blockquote{border-left:4px solid #6d83d8;margin-left:0;padding-left:16px;color:#59657b}
"""

REPORT_STYLE = """
:root{--bg:#f5f5f3;--surface:#fff;--ink:#20262d;--muted:#68717b;--line:#d9dcdf;--accent:#c7650e;--accent-soft:#fff3e7;--green:#2f7c50}
*{box-sizing:border-box;min-width:0}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI","Noto Sans SC","Microsoft YaHei",Arial,sans-serif;font-size:16px;line-height:1.75}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.report-topbar{height:58px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 22px;position:relative;z-index:10}
.report-brand{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:750}.report-brand:hover{text-decoration:none}.report-brand>span:first-child{width:32px;height:32px;border-radius:5px;background:var(--accent);color:#fff;display:grid;place-items:center}.report-actions{display:flex;align-items:center;gap:8px}.report-actions a,.report-actions button{height:34px;border:1px solid var(--line);border-radius:4px;background:#fff;color:#4e5760;padding:0 11px;display:inline-flex;align-items:center;gap:7px;font:600 12px inherit;cursor:pointer}.report-actions button:hover,.report-actions a:hover{background:#faf9f7;text-decoration:none}.toc-toggle{display:none!important}
.report-actions button.saved{border-color:#d49a67;background:var(--accent-soft);color:var(--accent)}
.report-shell{max-width:1920px;margin:0 auto;display:grid;grid-template-columns:270px minmax(0,1fr);gap:24px;justify-content:stretch;padding:42px 20px 72px}.report-sidebar{position:sticky;top:18px;align-self:start;max-height:calc(100vh - 36px);overflow:auto;padding:0 10px 0 0}.toc-title{font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--muted);margin:0 0 10px;text-transform:uppercase}.toc-doc-title{font-size:13px;line-height:1.4;font-weight:750;margin:0 0 17px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.report-toc ul{list-style:none;margin:0;padding:0}.report-toc li{margin:0}.report-toc a{display:block;color:#5d6670;border-left:2px solid var(--line);padding:6px 10px;font-size:12px;line-height:1.35}.report-toc a:hover{color:var(--accent);border-left-color:var(--accent);text-decoration:none;background:var(--accent-soft)}.report-toc ul ul a{padding-left:22px;font-size:11px;color:#7a828b}
.report-article{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:56px 64px;overflow:hidden}.report-article h1,.report-article h2,.report-article h3,.report-article h4{scroll-margin-top:24px}.report-article>h1:first-child{font-size:36px;line-height:1.22;letter-spacing:-.02em;margin:0 0 38px;overflow-wrap:anywhere}.report-article h2{font-size:24px;line-height:1.3;margin:48px 0 18px;padding-top:4px;border-top:1px solid var(--line);padding-top:26px}.report-article h3{font-size:18px;line-height:1.4;margin:30px 0 12px}.report-article h4{font-size:16px;margin:24px 0 10px}.report-article p{margin:0 0 15px}.report-article ul,.report-article ol{padding-left:1.5em;margin:10px 0 18px}.report-article li{margin:6px 0}.report-article blockquote{margin:18px 0;border-left:3px solid var(--accent);background:#faf9f7;padding:12px 16px;color:#535c65}.report-article blockquote p:last-child{margin-bottom:0}
.report-article table{border-collapse:collapse;width:100%;margin:18px 0 28px;font-size:14px;display:table}.report-article th,.report-article td{border:1px solid var(--line);padding:10px 12px;text-align:left;vertical-align:top;overflow-wrap:anywhere}.report-article th{background:#f7f7f5;font-weight:750}.report-article tr:nth-child(even) td{background:#fcfcfb}.report-article code{background:#f0f1f1;border-radius:3px;padding:2px 5px;font:90% Consolas,"SFMono-Regular",monospace}.report-article pre{overflow:auto;background:#242a30;color:#f1f3f4;padding:16px;border-radius:5px;font-size:13px}.report-article pre code{background:transparent;padding:0}.report-article img{display:block;max-width:100%;height:auto;margin:24px auto 12px;border:1px solid var(--line);border-radius:5px}.report-article img+em{display:block;text-align:center;color:var(--muted);font-size:13px}
.math-block{margin:22px 0;overflow-x:auto;overflow-y:hidden;text-align:center;padding:12px 4px}.math-inline{white-space:nowrap}.katex{font-size:1.08em}.math-block .katex-display{margin:0}.math-error{color:#a33;background:#fff1f1;padding:8px;border-radius:4px}
@media(max-width:1050px){.report-shell{grid-template-columns:210px minmax(0,1fr);gap:16px;padding:34px 14px 60px}.report-article{padding:42px 38px}.report-article>h1:first-child{font-size:31px}}
@media(max-width:760px){html{scroll-padding-top:16px}body{font-size:15px}.report-topbar{padding:0 12px}.report-actions>a{display:none}.toc-toggle{display:inline-flex!important}.report-shell{display:block;padding:22px 10px 48px}.report-sidebar{display:none;position:fixed;inset:58px 0 0 0;max-height:none;background:rgba(245,245,243,.98);padding:22px;z-index:25}.report-sidebar.open{display:block}.report-article{padding:34px 20px}.report-article h1,.report-article h2,.report-article h3,.report-article h4{scroll-margin-top:16px}.report-article>h1:first-child{font-size:27px;margin-bottom:28px}.report-article h2{font-size:21px;margin-top:38px}.report-article table{display:block;overflow-x:auto;white-space:normal}.report-article th,.report-article td{min-width:130px}}
@media print{.report-topbar,.report-sidebar{display:none!important}.report-shell{display:block;padding:0}.report-article{border:0;padding:0}.report-article h2{break-after:avoid}.report-article img,.math-block{break-inside:avoid}}
"""

MATH_BLOCK_PATTERNS = (
    re.compile(r"\\\[(.+?)\\\]", re.S),
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?)\}(.+?)\\end\{\1\}", re.S),
)
MATH_INLINE_PATTERNS = (
    re.compile(r"\\\((.+?)\\\)", re.S),
    re.compile(r"(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$"),
)


def _protect_math(markdown_text: str) -> tuple[str, dict[str, tuple[bool, str]]]:
    tokens: dict[str, tuple[bool, str]] = {}

    def replace_block(match: re.Match[str]) -> str:
        token = f"MATHBLOCKTOKEN{len(tokens)}END"
        tex = match.group(2) if match.lastindex and match.lastindex > 1 else match.group(1)
        tokens[token] = (True, tex.strip())
        return f"\n\n{token}\n\n"

    protected = markdown_text
    for pattern in MATH_BLOCK_PATTERNS:
        protected = pattern.sub(replace_block, protected)

    def replace_inline(match: re.Match[str]) -> str:
        token = f"MATHINLINETOKEN{len(tokens)}END"
        tokens[token] = (False, match.group(1).strip())
        return token

    for pattern in MATH_INLINE_PATTERNS:
        protected = pattern.sub(replace_inline, protected)
    return protected, tokens


def markdown_with_math(markdown_text: str) -> tuple[str, str]:
    protected, tokens = _protect_math(markdown_text)
    converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc"],
        extension_configs={"toc": {"toc_depth": "2-3"}},
    )
    body = converter.convert(protected)
    for token, (display, tex) in tokens.items():
        escaped = html.escape(tex)
        replacement = (
            f'<div class="math-block" data-display="true">{escaped}</div>'
            if display
            else f'<span class="math-inline" data-display="false">{escaped}</span>'
        )
        body = body.replace(f"<p>{token}</p>", replacement).replace(token, replacement)
    return body, converter.toc


def render_report(
    markdown_text: str,
    destination: Path,
    title: str,
    arxiv_id: str = "",
) -> None:
    body, toc = markdown_with_math(markdown_text)
    escaped_title = html.escape(title)
    library_action = ""
    if arxiv_id:
        library_action = (
            '<button id="report-library-add" type="button" '
            f'data-arxiv-id="{html.escape(arxiv_id, quote=True)}">'
            '<i class="far fa-bookmark" aria-hidden="true"></i>'
            '<span>加入文献库</span></button>'
        )
    destination.write_text(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{escaped_title}</title>
<link rel="stylesheet" href="/static/vendor/fontawesome/css/all.min.css">
<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">
<style>{REPORT_STYLE}</style></head><body>
<header class="report-topbar"><a class="report-brand" href="/reports"><span><i class="fas fa-book-open" aria-hidden="true"></i></span><span>arXiv Research Assistant</span></a>
<div class="report-actions"><a href="/reports"><i class="fas fa-arrow-left" aria-hidden="true"></i>返回报告库</a>{library_action}<button type="button" onclick="window.print()"><i class="fas fa-print" aria-hidden="true"></i>打印</button><button class="toc-toggle" type="button" aria-label="展开目录" aria-expanded="false"><i class="fas fa-list" aria-hidden="true"></i>目录</button></div></header>
<div class="report-shell"><aside class="report-sidebar"><p class="toc-title">CONTENTS</p><p class="toc-doc-title">{escaped_title}</p><nav class="report-toc" aria-label="报告目录">{toc}</nav></aside><article class="report-article">{body}</article></div>
<script src="/static/vendor/katex/katex.min.js"></script><script>
if('scrollRestoration' in history){{history.scrollRestoration='manual';}}if(!location.hash){{requestAnimationFrame(function(){{window.scrollTo(0,0);}});}}
document.querySelectorAll('.math-block,.math-inline').forEach(function(node){{try{{katex.render(node.textContent,node,{{displayMode:node.dataset.display==='true',throwOnError:false,strict:'ignore',trust:false}});}}catch(error){{node.classList.add('math-error');}}}});
var toggle=document.querySelector('.toc-toggle'),sidebar=document.querySelector('.report-sidebar');if(toggle&&sidebar){{toggle.addEventListener('click',function(){{var open=sidebar.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));}});sidebar.querySelectorAll('a').forEach(function(link){{link.addEventListener('click',function(){{sidebar.classList.remove('open');toggle.setAttribute('aria-expanded','false');}});}});}}
var libraryButton=document.querySelector('#report-library-add');if(libraryButton){{var libraryId=libraryButton.dataset.arxivId;var markSaved=function(){{libraryButton.classList.add('saved');libraryButton.disabled=true;libraryButton.querySelector('i').className='fas fa-thumbs-up';libraryButton.querySelector('span').textContent='已加入文献库（相关）';}};fetch('/api/library/status?arxiv_id='+encodeURIComponent(libraryId)).then(function(response){{return response.json();}}).then(function(payload){{if(payload.saved)markSaved();}}).catch(function(){{}});libraryButton.addEventListener('click',function(){{libraryButton.disabled=true;var data=new FormData();data.set('arxiv_id',libraryId);fetch('/api/library/add',{{method:'POST',body:data}}).then(function(response){{return response.json().then(function(payload){{if(!response.ok)throw new Error(payload.detail||'加入失败');return payload;}});}}).then(function(){{markSaved();}}).catch(function(){{libraryButton.disabled=false;}});}});}}
</script></body></html>""",
        encoding="utf-8",
    )


def render_digest(artifacts: list[ReportArtifact], destination: Path, date_label: str) -> str:
    cards: list[str] = []
    for artifact in artifacts:
        paper = artifact.paper
        report_html = artifact.report_path.with_suffix(".html")
        relative = report_html.relative_to(destination.parent).as_posix()
        authors = ", ".join(author.name for author in paper.authors[:5])
        venue = artifact.metadata.venue or "会议/期刊未核实"
        cards.append(
            f"""<main class='paper'><div><h2><a href='{html.escape(relative)}'>{html.escape(paper.title)}</a></h2>
<div class='meta'>{html.escape(authors)} · {html.escape(venue)} · arXiv:{html.escape(paper.arxiv_id)}</div>
<p>{html.escape(paper.recommendation_reason or paper.abstract[:260])}</p>
<a href='{html.escape(paper.abs_url)}'>arXiv</a> · <a href='{html.escape(relative)}'>阅读报告</a></div>
<div class='score'>{paper.final_score:.1f}</div></main>"""
        )
    body = "\n".join(cards) or "<main><p>本次没有符合条件的新论文。</p></main>"
    page = f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>arXiv 科研日报 {date_label}</title><style>{STYLE}</style><body><h1>arXiv 科研日报 · {date_label}</h1>{body}</body></html>"
    destination.write_text(page, encoding="utf-8")
    return page


def render_recommendations(
    papers: list[Paper],
    metadata: dict[str, VerifiedMetadata],
    destination: Path,
    date_label: str,
) -> str:
    cards: list[str] = []
    for paper in papers:
        verified = metadata.get(paper.arxiv_id, VerifiedMetadata())
        authors = ", ".join(author.name for author in paper.authors[:6])
        venue = verified.venue or "会议/期刊未核实"
        reason = paper.recommendation_reason or "依据研究类别、关键词和发布时间完成自动筛选。"
        cards.append(
            f"""<main class='paper'><div><h2><a href='{html.escape(paper.abs_url)}'>{html.escape(paper.title)}</a></h2>
<div class='meta'>{html.escape(authors)} · {html.escape(venue)} · arXiv:{html.escape(paper.arxiv_id)}</div>
<p><strong>推荐理由：</strong>{html.escape(reason)}</p>
<p><strong>摘要：</strong>{html.escape(paper.abstract[:900])}</p>
<a href='{html.escape(paper.abs_url)}'>摘要页</a> · <a href='{html.escape(paper.pdf_url)}'>PDF</a></div>
<div class='score'>{paper.final_score:.1f}</div></main>"""
        )
    body = "\n".join(cards) or "<main><p>本次没有符合条件的新论文。</p></main>"
    page = f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>arXiv 每日推荐 {date_label}</title><style>{STYLE}</style><body><h1>arXiv 每日推荐 · {date_label}</h1><p>深度阅读报告不会自动生成；需要时请使用 arXiv ID 主动生成并保存在本地。</p>{body}</body></html>"
    destination.write_text(page, encoding="utf-8")
    return page
