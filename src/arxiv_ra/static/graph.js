'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const data = JSON.parse($('#graph-data').textContent);
  const nodes = data.nodes.map(n => ({...n, x: 0, y: 0, vx: 0, vy: 0}));
  const byId = new Map(nodes.map(n => [n.paperId, n]));
  const seed = nodes.find(n => n.roles.includes('seed')) || nodes[0];
  const edges = [...(data.edges || []), ...(data.citation_edges || [])]
    .map(e => ({...e, a: byId.get(e.source), b: byId.get(e.target)}))
    .filter(e => e.a && e.b && e.a !== e.b && (e.kind !== 'similarity' || Number(e.score) >= .025));
  const sourceNames = {seed: '起点论文', references: '参考文献', citations: '引用该论文', similar: '近期推荐', cross: '交叉引用', conditions: '方向条件'};
  const roles = {seed: '起点论文', reference: '参考文献', citation: '引用该论文', similar: '服务推荐'};
  const statusNames = {ok: '已获取', empty: '成功 · 无结果', partial: '部分可用', cached: '使用缓存', failed: '获取失败', unknown: '历史状态未知'};
  const textNames = {full: '标题与摘要', short_abstract: '摘要较短，依据有限', title_only: '缺少摘要，仅按标题计算', unsupported: '缺少可计算文本，内容相似未知'};
  const workspace = $('#workspace');
  let selected = seed, selectedEdge = null, mode = 'content', query = '', allRelations = false, labels = false;
  let showSimilarity = true, showCitations = false, transform = {x: 0, y: 0, k: 1};
  let drawerReturn = null, drag = null, moved = false;
  const hasNumber = value => value !== null && value !== undefined && Number.isFinite(Number(value));
  const citations = n => hasNumber(n.citationCount) ? Number(n.citationCount).toLocaleString() : '未知';
  const radius = n => n === seed ? 20 : 8 + Math.min(13, Math.log1p(Math.max(0, Number(n.citationCount) || 0)) * 1.7);
  const years = nodes.map(n => n.year).filter(y => Number.isInteger(y) && y > 0);
  const minYear = Math.min(...years), maxYear = Math.max(...years);
  function element(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function safeUrl(value) {
    try { const url = new URL(String(value)); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
    catch { return ''; }
  }
  function paperUrl(n) {
    return n.externalIds?.ArXiv ? `https://arxiv.org/abs/${encodeURIComponent(n.externalIds.ArXiv)}` : safeUrl(n.url);
  }
  function date(value) { return value ? String(value).replace('T', ' ').slice(0, 16) : '未知'; }
  function announce(value) { $('#announcement').textContent = value; }
  $('#node-total').textContent = nodes.length;
  $('#map-meta').textContent = `${data.profile_name || '起点探索'}  ·  ${date(data.generated_at)}  ·  ${data.candidate_count ?? Math.max(0, nodes.length - 1)} 篇候选`;
  $('#year-legend').textContent = years.length ? `${minYear} — ${maxYear}` : '年份未知';
  $('#source-open').textContent = data.status === 'complete' ? '● 数据已获取' : data.status === 'legacy' ? '◷ 历史数据' : '◐ 部分数据';
  $('#source-open').classList.toggle('partial', data.status !== 'complete');
  $('#source-summary').textContent = `快照时间：${date(data.generated_at)}。${data.budget ? `已使用 ${data.budget.requests_used} / ${data.budget.max_requests} 次请求，展示 ${nodes.length} / ${data.budget.max_nodes} 个节点。` : '历史快照没有预算记录。'}${data.undisplayed_count ? `另有 ${data.undisplayed_count} 篇合格候选未展示。` : ''}`;
  for (const [name, status] of Object.entries(data.sources || {})) {
    const row = element('div', undefined, 'source-row');
    row.append(element('strong', sourceNames[name] || name), element('span', (name === 'conditions' && status.status === 'unknown' ? '待判断' : statusNames[status.status] || '未知') + (status.truncated ? ' · 已截断' : '')));
    row.append(element('p', `数据获取：${date(status.fetched_at)} · 本次尝试：${date(status.attempted_at)}${status.count !== undefined ? ` · ${status.count} 条` : ''}`));
    if (status.message) row.append(element('p', status.message));
    $('#source-list').append(row);
  }
  for (const [button, dialog] of [['#source-open', '#sources'], ['#help-open', '#help'], ['#filter-open', '#filters']]) {
    $(button).addEventListener('click', () => $(dialog).showModal());
  }
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));

  function openDetail() {
    workspace.classList.remove('detail-collapsed');
    if (window.innerWidth < 1200) {
      if (!workspace.classList.contains('drawer-open')) drawerReturn = document.activeElement;
      workspace.classList.add('drawer-open');
      $('#detail-panel').setAttribute('role', 'dialog');
      $('#detail-panel').setAttribute('aria-modal', 'true');
      $('.list-panel').inert = true;
      $('.graph-panel').inert = true;
      $('#detail-close').focus();
    }
  }
  function closeDetail() {
    workspace.classList.remove('drawer-open');
    workspace.classList.add('detail-collapsed');
    $('#detail-panel').removeAttribute('role');
    $('#detail-panel').removeAttribute('aria-modal');
    $('.list-panel').inert = false;
    $('.graph-panel').inert = false;
    if (drawerReturn?.isConnected) drawerReturn.focus(); else $('#detail-open').focus();
  }
  $('#detail-open').addEventListener('click', openDetail);
  $('#detail-close').addEventListener('click', closeDetail);
  document.addEventListener('keydown', event => {
    if (!workspace.classList.contains('drawer-open') || document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') { event.preventDefault(); closeDetail(); }
    if (event.key === 'Tab') {
      const focusable = [...$('#detail-panel').querySelectorAll('button,a[href],input,select')].filter(el => !el.disabled);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 1200 && workspace.classList.contains('drawer-open')) {
      workspace.classList.remove('drawer-open');
      $('.list-panel').inert = $('.graph-panel').inert = false;
      $('#detail-panel').removeAttribute('role'); $('#detail-panel').removeAttribute('aria-modal');
    }
  });
  $('#collapse-list').addEventListener('click', () => { workspace.classList.remove('mobile-show-list'); workspace.classList.add('list-collapsed'); $('#expand-list').focus(); });
  $('#expand-list').addEventListener('click', () => workspace.classList.remove('list-collapsed'));
  $('#mobile-list').addEventListener('click', () => { workspace.classList.remove('list-collapsed'); workspace.classList.add('mobile-show-list'); $('#paper-search').focus(); });

  function visible(n) {
    if (n === seed) return true;
    const allowed = [...document.querySelectorAll('input[name=role]:checked')].map(input => input.value);
    const from = Number($('#year-from').value) || 0, to = Number($('#year-to').value) || Infinity;
    const yearOk = hasNumber(n.year) ? Number(n.year) >= from && Number(n.year) <= to : $('#unknown-year').checked;
    const minimum = Math.max(0, Number($('#min-citations').value) || 0);
    const countOk = minimum === 0 || hasNumber(n.citationCount) && Number(n.citationCount) >= minimum;
    return n.roles.some(role => allowed.includes(role)) && yearOk && countOk
      && `${n.title} ${(n.authors || []).map(a => a.name).join(' ')}`.toLocaleLowerCase().includes(query);
  }
  function activeEdge(e) { return visible(e.a) && visible(e.b) && (e.kind === 'similarity' ? showSimilarity : showCitations); }
  function neighbors() {
    const found = new Set([selected.paperId]);
    for (const edge of edges.filter(activeEdge)) if (edge.a === selected || edge.b === selected) { found.add(edge.source); found.add(edge.target); }
    return found;
  }
  function card(n) {
    const button = element('button', undefined, 'paper-card');
    button.dataset.paperId = n.paperId;
    button.setAttribute('aria-pressed', String(n === selected));
    button.classList.toggle('active', n === selected);
    const top = element('div', undefined, 'card-top');
    top.append(element('span', n.roles.includes('seed') ? '起点论文' : (n.roles.map(role => roles[role] || role).join(' · ')), `role-tag${n === seed ? ' seed' : ''}`), element('span', String(n.year || '年份未知')));
    button.append(top, element('h3', n.title || '无标题'), element('p', `${(n.authors || []).slice(0, 2).map(a => a.name).join(', ') || '作者未知'} · 引用 ${citations(n)}`));
    if (n !== seed && n.selection_reason) button.append(element('p', n.selection_reason, 'card-reason'));
    button.addEventListener('click', () => select(n, true));
    return button;
  }
  function renderList() {
    const list = $('#paper-list');
    list.replaceChildren();
    const key = $('#sort').value;
    const filtered = nodes.filter(visible).sort((a, b) => {
      if (a === seed || b === seed) return a === seed ? -1 : 1;
      const value = n => key === 'year' ? Number(n.year) || 0 : key === 'citations' ? Number(n.citationCount) || 0 : Number(n.relevance) || 0;
      return value(b) - value(a) || String(a.paperId).localeCompare(String(b.paperId));
    });
    filtered.forEach(n => list.append(card(n)));
    $('#visible-count').textContent = `${filtered.length - 1} / ${nodes.length - 1} 篇 · 另含起点`;
    if (filtered.length === 1) list.append(element('p', '没有符合条件的关联论文，起点仍保留为参照。', 'empty-list'));
  }
  function relatedButton(edge, current) {
    const other = edge.a === current ? edge.b : edge.a;
    const button = element('button', other.title || '无标题', 'relation-button');
    button.append(element('small', edge.kind === 'similarity' ? '内容相似 · 查看共同词与参考依据 →' : `${edge.a === current ? '引用了' : '被引用于'} · 查看引用方向 →`));
    button.addEventListener('click', () => selectEdge(edge));
    return button;
  }
  function section(title) {
    const el = element('section', undefined, 'detail-section');
    el.append(element('h3', title)); return el;
  }
  function renderDetail(n) {
    $('#detail-eyebrow').textContent = '论文详情';
    const content = $('#detail-content'); content.replaceChildren();
    content.append(element('span', n.roles.map(role => roles[role] || role).join(' · '), 'role-tag'), element('h2', n.title || '无标题'));
    content.append(element('p', (n.authors || []).map(a => a.name).join(', ') || '作者未知', 'authors'));
    content.append(element('div', `${n.year || '年份未知'} · ${n.venue || '出版信息未核实'} · 引用 ${citations(n)}`, 'detail-meta'));
    if (n.eligibility_note) content.append(element('p', n.eligibility_note, 'evidence-box warning'));
    if (n.condition_checks?.length) {
      const checks = section('方向条件');
      for (const check of n.condition_checks) {
        checks.append(element('p', `${check.text} · ${{satisfied:'满足',not_satisfied:'不满足',unknown:'待判断'}[check.verdict] || '待判断'}`));
        if (check.quote) checks.append(element('p', `原文依据：${check.quote}`, 'evidence-box'));
        if (check.reason) checks.append(element('p', check.reason, 'muted'));
      }
      content.append(checks);
    }
    const evidence = section('为什么相关');
    evidence.append(element('p', n === seed ? '这是本次探索的参照论文。选择周围节点，查看它们与起点或其他工作的关系。' : n.selection_reason || '历史快照未记录入选依据。'));
    evidence.append(element('p', textNames[n.text_status] || '文本来源状态未知', 'muted'));
    const related = edges.filter(edge => (edge.a === n || edge.b === n) && visible(edge.a) && visible(edge.b));
    related.forEach(edge => evidence.append(relatedButton(edge, n)));
    if (!related.length) evidence.append(element('p', '暂未发现可展示的可靠关系。推荐来源本身不构成引用或内容相似证据。', 'muted'));
    content.append(evidence);
    const abstract = section('摘要'); abstract.append(element('p', n.abstract || '暂无摘要。')); content.append(abstract);
    const url = paperUrl(n);
    if (url) { const link = element('a', '打开论文 ↗', 'paper-link'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link); }
    const semantic = safeUrl(n.url);
    if (semantic && semantic !== url) { const link = element('a', '在 Semantic Scholar 查看 ↗', 'secondary-link'); link.href = semantic; link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link); }
  }
  function select(n, show = false) {
    selected = n; selectedEdge = null; renderDetail(n);
    document.querySelectorAll('.paper-card').forEach(button => { const on = button.dataset.paperId === n.paperId; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); });
    applyGraph(); if (show) openDetail();
  }
  function selectEdge(edge) {
    selectedEdge = edge;
    const content = $('#detail-content'); content.replaceChildren();
    $('#detail-eyebrow').textContent = '关联依据';
    content.append(element('span', edge.kind === 'similarity' ? '内容相似（词汇估计）' : '真实引用关系', 'role-tag'));
    for (const [index, n] of [edge.a, edge.b].entries()) {
      if (index) content.append(element('p', edge.kind === 'similarity' ? '与' : '↓ 引用了', 'eyebrow'));
      const button = element('button', n.title, 'relation-button'); button.addEventListener('click', () => select(n)); content.append(button);
    }
    if (edge.kind === 'similarity') {
      content.append(element('p', `当前快照相似分数 ${Number(edge.score).toFixed(3)}；不是概率，不宜跨图比较。`, 'evidence-box'));
      const words = element('div', undefined, 'terms'); (edge.terms || []).forEach(term => words.append(element('span', term))); content.append(words);
      if (!edge.terms?.length) content.append(element('p', '历史快照未记录共同词，请重新生成以查看依据。', 'muted'));
      const shared = section('已获取数据中的共同参考文献');
      (edge.shared_references || []).forEach(ref => shared.append(element('p', ref.title || ref.paperId)));
      if (!edge.shared_references?.length) shared.append(element('p', '当前数据中没有可展示的共同参考文献；这不能证明不存在共同研究基础。', 'muted'));
      if (edge.shared_reference_count > (edge.shared_references || []).length) shared.append(element('p', `共 ${edge.shared_reference_count} 篇，此处展示前 8 篇。`, 'muted'));
      content.append(shared);
    } else {
      content.append(element('p', `来源：Semantic Scholar · ${statusNames[edge.source_status] || '历史状态未知'} · 获取时间：${date(edge.fetched_at)}`, 'evidence-box'));
      content.append(element('p', '箭头由引用方指向被引用方。引用记录本身不能证明支持、反驳或方法继承。', 'muted'));
    }
    applyGraph(); openDetail();
  }
  const pending = data.pending || [];
  $('.pending').hidden = !pending.length;
  $('#pending-summary').textContent = `待判断候选 · ${pending.length}`;
  pending.forEach(n => { const button = element('button', n.title); button.addEventListener('click', () => { renderDetail(n); openDetail(); }); $('#pending-list').append(button); });

  const svg = $('#graph'), viewport = $('#viewport'), NS = 'http://www.w3.org/2000/svg';
  function svgElement(tag, attributes) {
    const el = document.createElementNS(NS, tag); for (const [key, value] of Object.entries(attributes || {})) el.setAttribute(key, String(value)); return el;
  }
  const hash = value => [...String(value)].reduce((acc, ch) => (acc * 31 + ch.charCodeAt(0)) >>> 0, 2166136261);
  const others = nodes.filter(n => n !== seed).sort((a, b) => hash(a.paperId) - hash(b.paperId) || a.paperId.localeCompare(b.paperId));
  seed.x = 500; seed.y = 360;
  others.forEach((n, i) => { const angle = i * Math.PI * (3 - Math.sqrt(5)); const r = 95 + Math.sqrt((i + .5) / Math.max(1, others.length)) * 260; n.x = 500 + Math.cos(angle) * r; n.y = 360 + Math.sin(angle) * r * .85; });
  const color = n => {
    if (n === seed) return '#c7650e';
    if (!hasNumber(n.year)) return '#b7beb6';
    const t = (Number(n.year) - minYear) / Math.max(1, maxYear - minYear);
    return `rgb(${[190, 204, 171].map((v, i) => Math.round(v + ([58, 102, 79][i] - v) * t)).join(',')})`;
  };
  const edgeElements = edges.map(edge => {
    const group = svgElement('g'), line = svgElement('line', {class: `edge-visible${edge.kind === 'similarity' ? '' : ' citation-line'}`, 'stroke-width': edge.kind === 'similarity' ? 1 + Math.min(1.8, edge.score * 3) : 1.6});
    const hit = svgElement('line', {class: 'edge-hit'});
    group.append(line, hit); $('#edges').append(group);
    hit.addEventListener('click', event => { event.stopPropagation(); selectEdge(edge); });
    return {edge, group, line, hit};
  });
  const nodeElements = nodes.map(n => {
    const group = svgElement('g');
    const halo = svgElement('circle', {r: radius(n) + 6, class: 'node-halo'});
    const circle = svgElement('circle', {r: radius(n), fill: color(n), class: 'node-circle'});
    const title = svgElement('title'); title.textContent = n.title;
    group.append(halo, circle, title); $('#nodes').append(group);
    const label = svgElement('text', {class: 'node-label', 'text-anchor': 'middle'});
    const author = (n.authors || [])[0]?.name?.split(/\s+/).slice(-1)[0];
    label.textContent = author ? `${author} · ${n.year || '?'}` : (n.title || '').slice(0, 30);
    $('#labels').append(label);
    circle.addEventListener('click', event => { event.stopPropagation(); if (!moved) select(n, true); });
    circle.addEventListener('pointerdown', event => { if (!layoutDone) return; moved = false; drag = {node: n, point: graphPoint(event), original: {x: n.x, y: n.y}, client: {x: event.clientX, y: event.clientY}}; circle.setPointerCapture(event.pointerId); event.stopPropagation(); });
    circle.addEventListener('pointermove', event => {
      if (drag?.node !== n) return;
      if (Math.hypot(event.clientX - drag.client.x, event.clientY - drag.client.y) > 4) moved = true;
      if (!moved) return;
      const point = graphPoint(event); n.x = drag.original.x + point.x - drag.point.x; n.y = drag.original.y + point.y - drag.point.y; renderPositions();
    });
    circle.addEventListener('pointerup', () => { drag = null; });
    circle.addEventListener('pointercancel', () => { drag = null; moved = true; });
    return {n, group, circle, halo, label};
  });
  function graphPoint(event) {
    const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(viewport.getScreenCTM().inverse());
  }
  function svgPoint(event) {
    const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  }
  function renderPositions() {
    for (const item of edgeElements) {
      const {edge, line, hit} = item, dx = edge.b.x - edge.a.x, dy = edge.b.y - edge.a.y;
      const distance = Math.hypot(dx, dy) || 1;
      const start = radius(edge.a) + 2, end = radius(edge.b) + 4;
      for (const el of [line, hit]) {
        el.setAttribute('x1', edge.a.x + dx / distance * start); el.setAttribute('y1', edge.a.y + dy / distance * start);
        el.setAttribute('x2', edge.b.x - dx / distance * end); el.setAttribute('y2', edge.b.y - dy / distance * end);
      }
    }
    for (const {n, group} of nodeElements) group.setAttribute('transform', `translate(${n.x} ${n.y})`);
    viewport.setAttribute('transform', `translate(${transform.x} ${transform.y}) scale(${transform.k})`);
    $('#zoom-level').textContent = `${Math.round(transform.k * 100)}%`;
    placeLabels();
  }
  function placeLabels() {
    const boxes = [];
    const near = neighbors();
    const sorted = [...nodeElements].sort((a, b) => (a.n === selected ? -2 : a.n === seed ? -1 : 0) - (b.n === selected ? -2 : b.n === seed ? -1 : 0));
    let neighborLabels = 0;
    for (const {n, label} of sorted) {
      const preferred = n === selected || n === seed;
      const show = visible(n) && (labels || preferred || near.has(n.paperId) && neighborLabels++ < 6);
      label.style.display = 'none'; if (!show) continue;
      const width = Math.max(35, label.textContent.length * 7), height = 17;
      let choice = null;
      for (const sign of [1, -1]) for (const offset of [0, 15, 30]) {
        if (choice) break;
        const box = {x: n.x - width / 2, y: n.y + sign * (radius(n) + 16 + offset) - height / 2, width, height};
        const overlaps = boxes.some(b => box.x < b.x + b.width + 6 && box.x + width + 6 > b.x && box.y < b.y + b.height + 3 && box.y + height + 3 > b.y);
        const disk = nodes.some(other => visible(other) && Math.hypot(other.x - Math.max(box.x, Math.min(other.x, box.x + width)), other.y - Math.max(box.y, Math.min(other.y, box.y + height))) < radius(other) + 3);
        if (!overlaps && !disk) choice = box;
      }
      if (!choice && preferred) choice = {x: n.x - width / 2, y: n.y + radius(n) + 18, width, height};
      if (choice) { boxes.push(choice); label.setAttribute('x', n.x); label.setAttribute('y', choice.y + height / 2); label.style.display = ''; }
    }
  }
  function applyGraph() {
    const near = neighbors();
    for (const {n, group, circle, halo} of nodeElements) {
      group.style.display = visible(n) ? '' : 'none';
      group.style.opacity = allRelations || near.has(n.paperId) || n === seed ? '1' : '.36';
      circle.classList.toggle('node-selected', n === selected); circle.classList.toggle('node-unknown', !hasNumber(n.year));
      halo.style.display = n === selected || n === seed ? '' : 'none';
    }
    for (const {edge, group, line} of edgeElements) {
      group.style.display = activeEdge(edge) ? '' : 'none';
      const focused = edge === selectedEdge || edge.a === selected || edge.b === selected;
      line.style.opacity = focused ? '.8' : allRelations ? '.26' : '.06';
    }
    placeLabels();
  }
  function applyFilters() {
    if (!visible(selected)) { selected = seed; selectedEdge = null; renderDetail(seed); announce('选中论文已被筛选隐藏，已返回起点。'); }
    if (selectedEdge && (!visible(selectedEdge.a) || !visible(selectedEdge.b))) { selectedEdge = null; renderDetail(selected); }
    renderList(); applyGraph();
    const count = nodes.filter(n => n !== seed && visible(n)).length;
    const message = $('#empty-state'); message.hidden = count > 0;
    if (!count) message.textContent = nodes.length > 1 ? '没有匹配当前筛选的论文。清除筛选可恢复地图。' : (data.pending?.length ? '候选方向条件尚待判断。可从列表查看候选与缺失依据。' : Object.entries(data.sources || {}).filter(([key]) => ['references', 'citations', 'similar'].includes(key)).every(([, value]) => value.status === 'failed') ? '候选来源暂时不可用。起点已保留，可稍后重新生成。' : '目前只有起点论文。查看数据状态以了解来源覆盖。');
    const filterCount = (query ? 1 : 0) + (3 - document.querySelectorAll('input[name=role]:checked').length) + (Number($('#year-from').value) ? 1 : 0) + (Number($('#year-to').value) ? 1 : 0) + (Number($('#min-citations').value) > 0 ? 1 : 0) + ($('#unknown-year').checked ? 0 : 1);
    $('#filter-count').textContent = filterCount ? `· ${filterCount}` : '';
  }
  $('#paper-search').addEventListener('input', event => { query = event.target.value.trim().toLocaleLowerCase(); applyFilters(); });
  $('#sort').addEventListener('change', renderList);
  $('#filters').querySelectorAll('input').forEach(input => input.addEventListener('input', applyFilters));
  $('#clear-filters').addEventListener('click', () => { query = ''; $('#paper-search').value = ''; $('#year-from').value = $('#year-to').value = ''; $('#min-citations').value = '0'; $('#unknown-year').checked = true; document.querySelectorAll('input[name=role]').forEach(input => input.checked = true); applyFilters(); });
  $('#toggle-all').addEventListener('change', event => { allRelations = event.target.checked; applyGraph(); });
  $('#toggle-labels').addEventListener('change', event => { labels = event.target.checked; placeLabels(); });
  $('#toggle-similarity').addEventListener('change', event => { showSimilarity = event.target.checked; applyGraph(); });
  $('#toggle-citations').addEventListener('change', event => { showCitations = event.target.checked; applyGraph(); });
  function setMode(value) {
    mode = value; showSimilarity = mode === 'content'; showCitations = !showSimilarity;
    $('#mode-content').setAttribute('aria-pressed', String(showSimilarity)); $('#mode-citations').setAttribute('aria-pressed', String(showCitations));
    $('#toggle-similarity').checked = showSimilarity; $('#toggle-citations').checked = showCitations;
    applyGraph();
  }
  $('#mode-content').addEventListener('click', () => setMode('content'));
  $('#mode-citations').addEventListener('click', () => setMode('citations'));
  function zoom(factor, point = {x: 500, y: 380}) {
    const previous = transform.k, next = Math.max(.35, Math.min(4, previous * factor));
    transform = {x: point.x - (point.x - transform.x) * next / previous, y: point.y - (point.y - transform.y) * next / previous, k: next}; renderPositions();
  }
  function fit() {
    const shown = nodes.filter(visible), left = Math.min(...shown.map(n => n.x - radius(n))) - 65, right = Math.max(...shown.map(n => n.x + radius(n))) + 65;
    const top = Math.min(...shown.map(n => n.y - radius(n))) - 55, bottom = Math.max(...shown.map(n => n.y + radius(n))) + 55;
    const k = Math.min(1.7, 930 / Math.max(1, right - left), 670 / Math.max(1, bottom - top));
    transform = {x: 500 - (left + right) / 2 * k, y: 380 - (top + bottom) / 2 * k, k}; renderPositions();
  }
  $('#zoom-in').addEventListener('click', () => zoom(1.2)); $('#zoom-out').addEventListener('click', () => zoom(1 / 1.2));
  $('#fit-view').addEventListener('click', fit);
  $('#center-seed').addEventListener('click', () => { select(seed); transform.x = 500 - seed.x * transform.k; transform.y = 380 - seed.y * transform.k; renderPositions(); });
  $('#reset-view').addEventListener('click', () => { transform = {x: 0, y: 0, k: 1}; renderPositions(); });
  svg.addEventListener('wheel', event => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1, svgPoint(event)); }, {passive: false});
  svg.addEventListener('pointerdown', event => { if (event.target.closest('.node-circle,.edge-hit')) return; const point = svgPoint(event); drag = {point, original: {...transform}}; svg.setPointerCapture(event.pointerId); });
  svg.addEventListener('pointermove', event => { if (!drag || drag.node) return; const point = svgPoint(event); transform.x = drag.original.x + point.x - drag.point.x; transform.y = drag.original.y + point.y - drag.point.y; renderPositions(); });
  svg.addEventListener('pointerup', () => { drag = null; }); svg.addEventListener('pointercancel', () => { drag = null; });

  let step = 0, calm = 0, layoutDone = false;
  const startTime = performance.now();
  const springs = edges.filter(edge => edge.kind === 'similarity');
  function simulate() {
    let energy = 0;
    for (const n of nodes) { n.vx += (500 - n.x) * .0009; n.vy += (360 - n.y) * .0009; }
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j], dx = a.x - b.x || .01, dy = a.y - b.y || .01, d = Math.hypot(dx, dy);
      const minimum = radius(a) + radius(b) + 25;
      const force = 1000 / (d * d + 100) + (d < minimum ? (minimum - d) * .025 : 0);
      a.vx += dx / d * force; a.vy += dy / d * force; b.vx -= dx / d * force; b.vy -= dy / d * force;
    }
    for (const edge of springs) {
      const dx = edge.b.x - edge.a.x, dy = edge.b.y - edge.a.y, d = Math.hypot(dx, dy) || 1;
      const strength = Math.min(1, edge.score), ideal = 220 - strength * 110, force = (d - ideal) * (.001 + strength * .004);
      edge.a.vx += dx / d * force; edge.a.vy += dy / d * force; edge.b.vx -= dx / d * force; edge.b.vy -= dy / d * force;
    }
    for (const n of nodes) {
      if (n === seed) { n.vx = n.vy = 0; continue; }
      n.vx = Math.max(-5, Math.min(5, n.vx * .78)); n.vy = Math.max(-5, Math.min(5, n.vy * .78));
      n.x = Math.max(60, Math.min(940, n.x + n.vx)); n.y = Math.max(50, Math.min(685, n.y + n.vy));
      energy += Math.abs(n.vx) + Math.abs(n.vy);
    }
    calm = energy / Math.max(1, nodes.length) < .06 ? calm + 1 : 0; step++;
  }
  function finishLayout(message) {
    layoutDone = true; fit(); applyGraph();
    $('#layout-status').textContent = message;
    document.body.dataset.layout = 'ready';
    document.body.dataset.layoutMs = String(Math.round(performance.now() - startTime));
  }
  function frame() {
    try {
      const deadline = performance.now() + 8;
      while (step < 280 && calm < 15 && performance.now() < deadline) simulate();
      if (step >= 280 || calm >= 15) { finishLayout('布局已就绪 · 拖动画布探索'); return; }
      if (performance.now() - startTime > 2700) { finishLayout('已保留当前稳定位置，可继续探索'); return; }
      requestAnimationFrame(frame);
    } catch { finishLayout('布局未完成，已保留初始位置与论文列表'); }
  }
  renderPositions(); renderDetail(seed); applyFilters();
  document.body.dataset.interactiveMs = String(Math.round(performance.now()));
  requestAnimationFrame(frame);
})();
