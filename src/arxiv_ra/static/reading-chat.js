(() => {
  'use strict';
  let host = document.querySelector('#reading-panel');
  const papers = document.querySelector('#report-papers');
  const reportButton = document.querySelector('#report-library-add');
  if (!host && !papers) return;
  if (!['http:', 'https:'].includes(location.protocol)) {
    if (reportButton) {
      const link = document.createElement('a');
      link.href = 'http://127.0.0.1:8000/reading?' + new URLSearchParams({arxiv_id: reportButton.dataset.arxivId, report_id: reportButton.dataset.reportId || ''});
      link.textContent = '在 PaperLoom 中继续对话（请先启动服务）';
      document.querySelector('.report-actions').append(link);
    }
    return;
  }
  if (!host) {
    host = document.createElement('aside'); host.id = 'report-reading'; host.hidden = true;
    host.setAttribute('aria-label', '论文阅读助手');
    Object.assign(host.dataset, reportButton ? reportButton.dataset : papers.dataset);
    document.body.append(host);
    document.addEventListener('reading-dock-opening', () => {
      if(!papers.hidden)document.querySelector('#report-papers-close').click();
    });
    const open = document.createElement('button'); open.className = 'reading-open'; open.textContent = '阅读对话 / 提问选区';
    open.setAttribute('aria-controls',host.id);open.setAttribute('aria-expanded','false');
    document.querySelector('.report-actions').append(open);
    open.onclick = async () => {
      if(!host.hidden){closeDock();return;}
      const selectedRange = window.getSelection();
      const article = document.querySelector('.report-article');
      const text = selectedRange && article.contains(selectedRange.anchorNode) && article.contains(selectedRange.focusNode) ? selectedRange.toString() : '';
      openDock();
      try {
        // The report remains the source even after browsing another paper's history.
        if (currentReport !== host.dataset.reportId || session?.archived) {
          saveDraft(); generation++; session = null;
          currentAid = host.dataset.arxivId; currentReport = host.dataset.reportId;
          input.value = ''; selection = ''; pictures = []; showPictures(); showSelection();
          await start();
        }
        if (text.trim()) setSelection(text.trim());
        input.focus();
      } catch(e) {error(e);}
    };
  }
  const reportHistory = host.id === 'report-reading';
  const view=createReadingView(host, reportHistory ? 'report' : 'workspace');
  const $ = selector => host.querySelector(selector);
  if(papers)installDock();
  const input = $('textarea'), status = $('.reading-status'), messages = $('.reading-messages');
  const compose = $('.reading-compose'), settingsForm = $('.reading-settings');
  const selectionBox = $('.reading-selection'), history = $('.reading-history');
  let session = null, selection = '', pictures = [], requestToken = null, busy = false, generation = 0, lastRender = '';
  let currentAid = host.dataset.arxivId || '', currentReport = host.dataset.reportId || '';
  let profile = '';
  const historyController=createConversationHistory({host,view,reportHistory,api,
    getContext:()=>({session,currentAid}),
    async onOpen(row) {
      saveDraft();generation++;session=row;
      currentAid=reportHistory ? row.paper.arxiv_id+'v'+row.paper.version : '';
      currentReport='';selection='';pictures=[];showPictures();input.value='';restoreDraft();
      lastRender='';await refresh();
      if(papers || innerWidth<850)setHistory(false);else await historyController.refresh();
    },
    onDeleted(id){if(session?.id===id)clearDeletedSession();},
    async onRenamed(id){if(session?.id===id)await refresh();},error,
  });
  const labels = {queued:'等待开始',running:'正在回答',completed:'',failed:'回答未完成',stopped:'已停止',interrupted:'已中断'};
  const expanded = new Set();
  function setHistory(open) {history.hidden = !open; host.classList.toggle('history-open', open); $('[data-action=history]').setAttribute('aria-expanded', String(open)); if(open) historyController.refresh().catch(error);}
  const settingsDialog = $('.reading-settings-dialog');
  $('[data-action=settings]').onclick = $('[data-action=model]').onclick = () => settingsDialog.showModal();
  $('[data-action=close-settings]').onclick = () => settingsDialog.close();
  $('[data-action=close-history]').onclick = () => setHistory(false);
  $('[data-action=attach]').onclick = () => compose.querySelector('[type=file]').click();
  input.addEventListener('keydown', event => {if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); if(!compose.querySelector('[type=submit]').disabled)compose.requestSubmit();}});
  host.querySelectorAll('[data-prompt]').forEach(button => {button.onclick = () => {input.value = button.dataset.prompt; input.oninput(); input.focus();};});
  const explainButton = $('[data-action=explain]');
  if (explainButton) explainButton.onclick = () => {if(!selection) {status.textContent='先在报告正文中选中文字，再点击“阅读对话 / 提问选区”。';return;} input.value='请结合原文解释这段选区，说明关键概念及其在论文中的作用。'; input.oninput(); input.focus();};
  const draftKey = () => 'paperloom.reading.draft:' + (session?.id || currentAid) + ':' + currentReport;
  function saveDraft() {try {sessionStorage.setItem(draftKey(), JSON.stringify({text: input.value, selection, report: currentReport}));} catch { /* optional */ }}
  function restoreDraft() {try {const d = JSON.parse(sessionStorage.getItem(draftKey()) || '{}'); input.value = d.text || ''; selection = d.report === currentReport ? d.selection || '' : ''; showSelection();} catch { /* optional */ }}
  function updateReferenceScroll() {
    if(reportHistory)return;
    const rail=$('.reading-reference-rail'), chips=$('.reading-reference-chips');
    const contentWidth=[...chips.children].reduce((width,chip)=>width+chip.getBoundingClientRect().width,0)+Math.max(0,chips.children.length-1)*6;
    rail.classList.toggle('has-overflow',contentWidth>rail.clientWidth+1);
    $('[data-reference-step="-1"]').disabled=chips.scrollLeft<1;
    $('[data-reference-step="1"]').disabled=chips.scrollLeft+chips.clientWidth>=chips.scrollWidth-1;
    rail.classList.toggle('can-scroll-left',chips.scrollLeft>=1);
    rail.classList.toggle('can-scroll-right',chips.scrollLeft+chips.clientWidth<chips.scrollWidth-1);
  }
  if(!reportHistory) {
    const chips=$('.reading-reference-chips');
    chips.addEventListener('scroll',updateReferenceScroll,{passive:true});
    new ResizeObserver(updateReferenceScroll).observe($('.reading-reference-rail'));
    host.querySelectorAll('[data-reference-step]').forEach(button=>{button.onclick=()=>chips.scrollBy({left:Number(button.dataset.referenceStep)*Math.max(146,chips.clientWidth*.8),behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});});
  }

  function renderReferences(value) {
    const refs = value.references || [];
    const active = value.messages.some(m=>['queued','running'].includes(m.status));
    $('.reading-reference-count').textContent = `引用 · ${refs.length}`;
    const chips = $('.reading-reference-chips'); chips.replaceChildren();
    for (const ref of refs) {
      const chip = textNode('div','','reading-reference-chip');
      const title = ref.paper.title + ' · v' + ref.paper.version;
      const label = textNode('span',title + (ref.missing ? ' · 报告缺失' : '')); label.title=title;
      chip.classList.toggle('missing',!!ref.missing);
      const remove = iconButton('移除引用：' + ref.paper.title, 'close');
      remove.disabled = active || busy || !!value.archived;
      remove.onclick = () => changeReferences(refs.filter(r=>r.report_id!==ref.report_id).map(r=>r.report_id)).catch(error);
      chip.append(label,remove); chips.append(chip);
    }
    requestAnimationFrame(updateReferenceScroll);
    $('[data-action=add-references]').disabled = active || busy || !!value.archived;
    $('.reading-reference-count').title = refs.length ? '仅查阅这些论文的报告与对应版本原文；移除引用不会删除历史讨论。' : '从报告库选择一篇或多篇论文，开始提问或交叉比较。';
  }
  async function changeReferences(reportIds) {
    if(!session)await start();
    const sid=session.id,ticket=generation;
    busy=true;render(session);
    try {
      const value=await api('/sessions/'+sid+'/references','PUT',{report_ids:reportIds, mode:'workspace'});
      if(session?.id===sid && generation===ticket){render(value);if(!history.hidden)await historyController.refresh();}
    } finally {busy=false;if(session)render(session);}
  }
  if(!reportHistory) {
    $('[data-action=new-workspace]').onclick=()=> $('[data-action=new]').click();
    const referenceEditor=createReferenceEditor({dialog:$('.reading-reference-dialog'),
      getContext:()=>({ticket:generation,references:session?.references || []}),
      loadCatalog:async()=>(await api('/reports')).reports,
      save:changeReferences,
    });
    $('[data-action=add-references]').onclick=()=>referenceEditor.open();
  }

  function showSelection() {selectionBox.hidden = !selection; selectionBox.querySelector('span').textContent = selection; if (explainButton) explainButton.disabled = !selection;}
  function setSelection(text) {selection = text.slice(0, 20000); showSelection(); saveDraft();}
  // Selection survives focus moving to the composer; only a new report range replaces it.
  if(papers)document.addEventListener('selectionchange',()=>{
    const range=window.getSelection(),article=document.querySelector('.report-article');
    if(!range || range.isCollapsed || !article || !article.contains(range.anchorNode) || !article.contains(range.focusNode))return;
    if(currentReport!==host.dataset.reportId || session?.archived)return;
    const text=range.toString().trim();
    if(text && text!==selection)setSelection(text);
  });
  selectionBox.querySelector('button').onclick = () => setSelection('');
  input.oninput = () => {requestToken = null; saveDraft();};
  async function api(path, method = 'GET', data) {
    const headers = {'Content-Type': 'application/json'};
    if (profile) headers['x-paperloom-profile'] = profile;
    const response = await fetch('/api/reading' + path, {method, headers, body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store'});
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '请求未通过校验，请检查输入');
    return value;
  }
  function error(e) {status.textContent = e.message; status.classList.add('reading-error');}
  function textNode(tag, text, cls) {const node = document.createElement(tag); node.textContent = text; if(cls) node.className = cls; return node;}
  function iconButton(label, name) {
    const button=textNode('button','','reading-icon-button');button.type='button';button.title=label;button.setAttribute('aria-label',label);
    const paths={copy:'M9 9h11v11H9z M15 5V3H3v12h2',undo:'M9 5 4 10l5 5 M4 10h10a6 6 0 0 1 6 6v3',close:'m6 6 12 12 M6 18 18 6',edit:'m16 3 5 5 M4 16 17 3l4 4L8 20H4z',trash:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7'};
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
    const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]);svg.append(path);button.append(svg);return button;
  }
  function openDock() {
    document.dispatchEvent(new Event('reading-dock-opening'));host.hidden=false;document.body.classList.add('reading-dock-open');
    document.querySelector('.reading-open')?.setAttribute('aria-expanded','true');
  }
  function closeDock(restoreFocus=true) {
    host.hidden=true;document.body.classList.remove('reading-dock-open');
    const toggle=document.querySelector('.reading-open');toggle?.setAttribute('aria-expanded','false');
    if(restoreFocus)toggle?.focus();
  }
  function installDock() {
    host.classList.add('reading-dock');
    const close=iconButton('收起阅读助手','close');close.onclick=()=>closeDock();
    $('.reading-header .reading-toolbar').append(close);
    const resize=document.createElement('div');resize.className='reading-dock-resize';resize.tabIndex=0;resize.setAttribute('role','separator');resize.setAttribute('aria-label','调整阅读助手宽度');resize.setAttribute('aria-orientation','vertical');
    document.body.append(resize);
    let width=480;try{width=Number(localStorage.getItem('paperloom.reading.width')) || width;}catch{}
    function size(value){width=Math.max(340,Math.min(Math.min(900,innerWidth),value));document.body.style.setProperty('--reading-width',width+'px');resize.setAttribute('aria-valuenow',Math.round(width));}
    function save(){try{localStorage.setItem('paperloom.reading.width',width);}catch{}}
    size(width);window.addEventListener('resize',()=>size(width));
    resize.onpointerdown=event=>{if(event.button!==0)return;event.preventDefault();resize.setPointerCapture(event.pointerId);const x=event.clientX,w=width;document.body.classList.add('reading-resizing');resize.onpointermove=e=>size(w+x-e.clientX);resize.onlostpointercapture=()=>{resize.onpointermove=null;document.body.classList.remove('reading-resizing');save();};};
    resize.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();size(event.key==='Home'?340:event.key==='End'?900:width+(event.key==='ArrowLeft'?20:-20));save();};
    document.addEventListener('keydown',e=>{if(e.key==='Escape' && !host.querySelector('dialog[open]'))close.click();});
    document.querySelector('#report-papers-toggle')?.addEventListener('click',()=>closeDock(false));
  }
  let eventSource=null, eventSid='', eventTicket=-1;
  function watchSession() {
    if(eventSid===session?.id && eventTicket===generation && eventSource)return;
    eventSource?.close();eventSource=null;eventSid=session?.id || '';eventTicket=generation;
    if(!session)return;
    const sid=session.id,ticket=generation;
    const source=new EventSource('/api/reading/sessions/'+sid+'/events');eventSource=source;
    const current=()=>session?.id===sid && generation===ticket;
    source.addEventListener('snapshot',e=>{if(current())render(JSON.parse(e.data));});
    source.addEventListener('update',e=>{if(!current())return;const update=JSON.parse(e.data),changed=new Map(update.messages.map(m=>[m.id,m]));render({...session,...update,messages:session.messages.map(m=>changed.get(m.id)||m)});});
    source.addEventListener('deleted',()=>{source.close();if(current()){clearDeletedSession();}});
    source.onerror=()=>{if(current())status.textContent='连接暂时中断，正在恢复实时更新…';};
  }
  function render(value) {
    if(session?.id===value.id && session.updated_at && value.updated_at && value.updated_at < session.updated_at)return;
    session = value;watchSession();
    view.renderPaper(value);
    if(!reportHistory)renderReferences(value);
    const active = value.messages.some(m => ['queued', 'running'].includes(m.status));
    compose.querySelector('[type=submit]').disabled = active || busy || !!value.archived || (!reportHistory && (!value.references?.length || value.references.some(r=>r.missing)));
    input.disabled=!!value.archived;
    if(value.archived)status.textContent='回退前归档 · 只读，可从历史记录查看完整内容';
    $('[data-action=stop]').disabled = !active;
    $('[data-action=stop]').hidden = !active;
    compose.querySelector('[type=submit]').hidden = active;
    const last = value.messages.at(-1);
    $('[data-action=retry]').disabled = !last || !['failed', 'stopped', 'interrupted'].includes(last.status);
    $('[data-action=retry]').hidden = $('[data-action=retry]').disabled;
    view.setSessionActions(!!session);
    const stamp = JSON.stringify(value.messages);
    status.classList.remove('reading-error');
    status.textContent = value.archived ? '回退前归档 · 只读' : active ? (last?.detail || '正在准备回答…') : '';
    if (!reportHistory && value.references?.some(r=>r.missing)) status.textContent = '引用报告已缺失，请移除失效引用或恢复对应版本报告。';
    if (stamp === lastRender) return;
    status.classList.toggle('is-active', active);
    lastRender = stamp;
    const nearEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    const oldNodes=new Map([...messages.querySelectorAll(':scope > .reading-message')].map(node=>[node.dataset.messageId,node]));
    messages.querySelector('.reading-welcome')?.remove();
    const valid=new Set(value.messages.map(m=>m.id));
    for(const [id,node] of oldNodes)if(!valid.has(id))node.remove();
    if(!value.messages.length) showWelcome();
    for (const message of value.messages) {
      const old=oldNodes.get(message.id),stamp=JSON.stringify(message);
      if(old?.dataset.stamp===stamp)continue;
      const box = textNode('div', '', 'reading-message ' + message.role);box.dataset.messageId=message.id;box.dataset.stamp=stamp;
      const meta = textNode('div', '', 'reading-message-meta');
      meta.append(textNode('span', message.role === 'user' ? 'YOU' : 'AI', 'reading-role'));
      if(message.role === 'assistant') meta.append(textNode('span', labels[message.status] || '', 'reading-message-state'));
      box.append(meta);
      if (!reportHistory && message.references?.length) box.append(textNode('small', '本轮引用：' + message.references.map(r=>r.paper.title + ' · v' + r.paper.version).join('；')));
      if(message.role === 'assistant' && (message.steps?.length || message.material || message.context)) {
        const trace = document.createElement('details'); trace.className='reading-trace';
        const key = message.id + ':trace'; trace.open=expanded.has(key) || ['running','queued'].includes(message.status);
        const material=message.material;
        trace.append(textNode('summary', '查阅过程' + (material?.total_pages ? ` · 已读 ${material.read_pages?.length || 0}/${material.total_pages} 页` : '')));
        if(message.context) {
          const c=message.context;
          trace.append(textNode('p', `上下文 ${c.messages} 条 · ${c.characters.toLocaleString()} 字符 · 图片 ${c.images} 张 · 工具 ${c.tools_used}/${c.tools_limit}${c.summarized ? ' · 含历史摘要' : ''}`, 'reading-context-info'));
        }
        for(const [stepIndex,step] of (message.steps || []).entries()) {
          const row=textNode('div', `${step.status === 'failed' ? '!' : step.status === 'running' ? '◌' : '✓'} ${step.label}${step.summary ? ' · '+step.summary : ''}`, 'reading-step '+step.status);
          if(step.fragments?.length){const pieces=document.createElement('details');const pieceKey=message.id+':fragments:'+stepIndex;pieces.open=expanded.has(pieceKey);pieces.ontoggle=()=>{if(pieces.open)expanded.add(pieceKey);else expanded.delete(pieceKey);};pieces.append(textNode('summary',`PDF 片段 · ${step.fragments.length} 段 / ${step.characters} 字符`));for(const f of step.fragments)pieces.append(textNode('blockquote',`第 ${f.page} 页 · ${f.characters} 字符
${f.text}`));row.append(pieces);}
          trace.append(row);
        }

        if(!message.reasoning)trace.append(textNode('small','当前接口尚未返回思考内容；此处展示实际工具执行记录。'));
        if(material?.source_note)trace.append(textNode('small',material.source_note));
        trace.ontoggle=()=>{if(trace.open)expanded.add(key);else expanded.delete(key);}; box.append(trace);
      }
      if(message.role === 'assistant' && message.reasoning) {
        const thinking=document.createElement('details');thinking.className='reading-reasoning';
        thinking.open=['running','queued'].includes(message.status) || expanded.has(message.id+':reasoning');
        const content=document.createElement('div');
        for(const paragraph of message.reasoning.trim().split(/\n\s*\n/)) {
          if(paragraph.trim())content.append(textNode('p',paragraph.trim()));
        }
        thinking.append(textNode('summary','思考过程 · 模型返回'),content);
        thinking.ontoggle=()=>{if(thinking.open)expanded.add(message.id+':reasoning');else expanded.delete(message.id+':reasoning');};box.append(thinking);
      }
      if (message.selection) box.append(textNode('blockquote', message.selection + (message.report?.missing ? '（来源报告已删除）' : '')));
      const body = document.createElement('div');body.className='reading-message-content';
      if (message.role === 'assistant' && message.html) body.innerHTML = message.html; // server-sanitized Markdown only
      else body.textContent = message.text;
      box.append(body);
      if(message.role === 'user') {
        const actions=textNode('div','','reading-answer-actions');
        const copy=iconButton('复制问题','copy');
        copy.onclick=()=>navigator.clipboard.writeText(message.text).then(()=>{copy.title='已复制';}).catch(()=>error(new Error('无法访问剪贴板，请手动选择文字复制')));
        actions.append(copy);box.append(actions);
      }
      if(message.role === 'assistant' && ['failed','stopped','interrupted'].includes(message.status))box.append(textNode('div',message.detail || labels[message.status],'reading-failure'));
      if(message.role === 'assistant' && !message.text && ['running','queued'].includes(message.status))box.append(textNode('div','正在查阅并整理回答…','reading-thinking'));
      if(message.role === 'assistant' && (message.text || !['queued','running'].includes(message.status))) {
        const actions=textNode('div','','reading-answer-actions'); const copy=iconButton('复制回答','copy');
        copy.onclick=()=>navigator.clipboard.writeText(message.text).then(()=>{copy.title='已复制';}).catch(()=>error(new Error('无法访问剪贴板，请手动选择文字复制')));
        const back=iconButton('回退到本轮提问之前','undo');back.disabled=active || !!value.archived;
        back.onclick=async()=>{
          if(!confirm('回退到本轮提问之前？当前完整对话将归档保留，原问题恢复到输入框。'))return;
          const sid=session.id,ticket=generation;
          try{const result=await api('/sessions/'+sid+'/rollback','POST',{message_id:message.id});if(session?.id!==sid || generation!==ticket)return;
            generation++;lastRender='';render(result.session);input.value=result.draft.text;selection=result.draft.selection || '';pictures=result.draft.images || [];currentReport=result.draft.report?.id || '';requestToken=null;showSelection();showPictures();saveDraft();input.focus();if(!history.hidden)await historyController.refresh();
          }catch(e){error(e);}
        };
        actions.append(copy,back,textNode('span',message.model || '')); box.append(actions);
      }
      for (const data of message.images || []) {const img = document.createElement('img'); img.src = data; img.alt = '本条问题图片'; box.append(img);}
      if (window.katex) box.querySelectorAll('.math-inline,.math-block').forEach(node => {
        try {katex.render(node.textContent,node,{displayMode:node.dataset.display==='true',throwOnError:false,trust:false});}catch{}
      });
      if(old){
        const before=old.querySelector('.reading-message-content'),after=box.querySelector('.reading-message-content');
        if(before && after){patchContent(before,after);after.replaceWith(before);}
        old.dataset.stamp=stamp;old.replaceChildren(...box.childNodes);
      }else messages.append(box);
    }
    if (nearEnd) messages.scrollTop = messages.scrollHeight;
  }
  // Keep existing paragraphs and text nodes while appending streamed text.
  function patchContent(target, source) {
    if(target.isEqualNode(source))return;
    if(target.nodeType!==Node.ELEMENT_NODE){target.nodeValue=source.nodeValue;return;}
    for(const attr of [...target.attributes])if(!source.hasAttribute(attr.name))target.removeAttribute(attr.name);
    for(const attr of source.attributes)if(target.getAttribute(attr.name)!==attr.value)target.setAttribute(attr.name,attr.value);
    [...source.childNodes].forEach((node,index)=>{
      const old=target.childNodes[index];
      if(!old)target.append(node.cloneNode(true));
      else if(old.nodeType!==node.nodeType || old.nodeName!==node.nodeName)old.replaceWith(node.cloneNode(true));
      else patchContent(old,node);
    });
    while(target.childNodes.length>source.childNodes.length)target.lastChild.remove();
  }
  function showWelcome() {
    view.showWelcome(prompt=>{input.value=prompt;input.oninput();input.focus();});
  }
  async function refresh() {
    if (!session) return;
    const sid = session.id, ticket = generation;
    const value = await api('/sessions/' + sid);
    if (ticket === generation && session?.id === sid) render(value);
  }
  async function start(fresh = false) {
    if (reportHistory && !currentAid) {historyController.showAll(); setHistory(true); messages.replaceChildren();showWelcome();return;}
    busy = true;
    const ticket = ++generation;
    status.textContent = '正在确认论文版本…';
    try {
      const value = await api('/sessions', 'POST', {arxiv_id: currentAid, report_id: currentReport, new: fresh,
        origin: host.dataset.origin || '', source_date: host.dataset.sourceDate || '', mode: reportHistory ? 'report' : 'workspace'});
      if (ticket !== generation) return;
      session = value; await refresh();
      if(!history.hidden)await historyController.refresh();
      // Keep a question drafted before version resolution.
      if (!input.value) restoreDraft();
    } catch(e) {
      if(!reportHistory && !session){currentAid='';currentReport='';messages.replaceChildren();showWelcome();}
      throw e;
    } finally {busy = false; if(session) render(session);}
  }
  $('[data-action=history]').onclick = () => setHistory(history.hidden);
  $('[data-action=new]').onclick = () => {if(!reportHistory){currentAid='';currentReport='';} saveDraft(); input.value = ''; selection = ''; pictures = []; showSelection(); showPictures(); start(true).catch(error);};
  function clearDeletedSession() {
    generation++;session=null;watchSession();input.value='';input.disabled=false;selection='';pictures=[];requestToken=null;
    showSelection();showPictures();messages.replaceChildren();showWelcome();lastRender='';status.textContent='会话已删除';
    if(!reportHistory){currentAid='';currentReport='';renderReferences({references:[],messages:[]});compose.querySelector('[type=submit]').disabled=true;}
    view.setSessionActions(false);
    setHistory(true);
  }
  view.bindSessionActions(()=>session && historyController.rename(session),()=>session && historyController.remove(session));
  $('[data-action=stop]').onclick = () => session && api('/sessions/'+session.id+'/stop','POST',{}).then(refresh).catch(error);
  async function send(payload, preserveDraft = false) {
    if(busy)return;
    const sid = session.id, ticket = generation;
    const submittedText = input.value, submittedSelection = selection, submittedPictures = pictures;
    busy=true; compose.querySelector('[type=submit]').disabled=true;
    try {
      await api('/sessions/'+sid+'/messages','POST',payload);
      if(ticket !== generation || session?.id !== sid)return;
      if(!preserveDraft) {
        if(input.value === submittedText)input.value='';
        if(selection === submittedSelection)selection='';
        if(pictures === submittedPictures)pictures=[];
      }
      requestToken=null; showSelection(); showPictures(); saveDraft(); await refresh();
      if(!history.hidden)await historyController.refresh();
    } finally {busy=false; if(session)render(session);}
  }
  compose.onsubmit = async event => {
    event.preventDefault(); if(!input.value.trim())return;
    try {if(!session)await start(); if(!session)return; requestToken ||= crypto.randomUUID();
      await send({text:input.value,request_id:requestToken,selection,report_id:currentReport,images:pictures});}catch(e){error(e);}
  };
  $('[data-action=retry]').onclick = () => {
    const user = session?.messages.filter(m=>m.role==='user').at(-1); if(!user)return;
    send({text:user.text, selection:user.selection, report_id:user.report?.id || '', images:user.images || [],
      request_id:crypto.randomUUID(),retry_of:user.id}, true).catch(error);
  };
  function showPictures() {
    const area=$('.reading-previews'); area.replaceChildren();
    pictures.forEach((data,index)=>{const wrap=document.createElement('div'); const img=document.createElement('img'); img.src=data; img.alt='待发送图片'; const remove=textNode('button','移除图片'); remove.type='button'; remove.onclick=()=>{pictures.splice(index,1); showPictures();}; wrap.append(img,remove); area.append(wrap);});
  }
  async function addFiles(files) {
    const ticket = generation, sid = session?.id;
    for(const file of files){
      if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw new Error('只支持 PNG、JPEG、WebP');
      if(file.size>5*1024*1024 || pictures.length>=4)throw new Error('最多四张图片，每张不超过 5 MiB');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});
      if(ticket !== generation || session?.id !== sid)return;
      pictures=[...pictures,data];
    } showPictures();
  }
  compose.querySelector('[type=file]').onchange=event=>addFiles(event.target.files).catch(error);
  input.addEventListener('paste',event=>{const files=[...event.clipboardData.items].filter(i=>i.kind==='file').map(i=>i.getAsFile()); if(files.length){event.preventDefault();addFiles(files).catch(error);}});
  settingsForm.onsubmit=async event=>{
    event.preventDefault();const form=new FormData(settingsForm);const body=Object.fromEntries(form);
    for(const key of ['independent','images','clear_key'])body[key]=form.has(key);
    for(const key of ['max_tools','max_tokens'])body[key]=Number(body[key]);
    try{const saved=await api('/settings','PUT',body);settingsForm.elements.api_key.value='';$('.reading-model-label').textContent=saved.model;settingsDialog.close();status.textContent='阅读设置已保存，应用于下一轮回答';}catch(e){error(e);}
  };
  restoreDraft();
  api('/settings').then(value=>{
    profile=value.profile_id;
    $('.reading-model-label').textContent=value.model;
    for(const [key,item] of Object.entries(value)){const field=settingsForm.elements.namedItem(key);if(field){if(field.type==='checkbox')field.checked=item;else field.value=item;}}
    return start().then(()=>{if(!papers && innerWidth >= 850)setHistory(true);});
  }).catch(error);
  window.addEventListener('pagehide',()=>eventSource?.close());
  window.addEventListener('pageshow',e=>{if(e.persisted){eventSource=null;watchSession();}});
  // An editor opening is a transaction: draft selection is committed only on Save.
  function createReferenceEditor({dialog, getContext, loadCatalog, save}) {
    const find=selector=>dialog.querySelector(selector);
    const search=find('input[type=search]'), list=find('.reading-reference-options');
    const all=find('[data-action=select-all-references]'), submit=find('[type=submit]');
    let draft=null, revision=0;
    function visibleChoices() {
      const query=search.value.trim().toLocaleLowerCase();
      return draft.catalog.filter(ref=>(ref.paper.title+' '+ref.paper.arxiv_id).toLocaleLowerCase().includes(query));
    }
    function selectable(ref) {return !ref.missing || draft.initial.has(ref.report_id);}
    function current(token) {return draft && token===revision && dialog.open;}
    function update() {
      if(!draft)return;
      const choices=visibleChoices().filter(selectable);
      const checked=choices.filter(ref=>draft.selected.has(ref.report_id)).length;
      const locked=draft.loading || draft.saving || !!draft.loadError;
      all.disabled=locked || !choices.length;
      all.checked=choices.length>0 && checked===choices.length;
      all.indeterminate=checked>0 && checked<choices.length;
      search.disabled=draft.saving;
      list.querySelectorAll('input').forEach(check=>{check.disabled=locked || check.dataset.selectable!=='true';check.checked=draft.selected.has(check.value);});
      find('.reading-reference-selection-count').textContent=`已选 ${draft.selected.size} 篇`;
      const changed=draft.initial.size!==draft.selected.size || [...draft.initial].some(id=>!draft.selected.has(id));
      submit.textContent=draft.initial.size ? '保存引用' : '加入所选';submit.disabled=locked || !changed;
    }
    function draw() {
      list.replaceChildren();
      if(draft.loading){list.textContent='正在加载报告库…';update();return;}
      for(const ref of visibleChoices()) {
        const row=textNode('label','','reading-reference-option');
        const check=document.createElement('input');check.type='checkbox';check.value=ref.report_id;
        check.dataset.selectable=String(selectable(ref));
        check.onchange=()=>{if(check.checked)draft.selected.add(ref.report_id);else draft.selected.delete(ref.report_id);update();};
        const body=textNode('span','');body.append(textNode('strong',ref.paper.title),textNode('small',`${ref.paper.arxiv_id} · v${ref.paper.version}${draft.initial.has(ref.report_id) ? ' · 已引用' : ref.missing ? ' · 报告不可读' : ''}`));
        row.append(check,body);list.append(row);
      }
      if(!list.childElementCount && !draft.loadError)list.append(textNode('p',search.value.trim() ? '没有匹配的报告论文' : '报告库中暂无可引用论文，请先生成报告。'));
      update();
    }
    find('[data-action=close-references]').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{if(!dialog.open){revision++;draft=null;}});
    all.onchange=()=>{
      if(!draft || all.disabled)return;
      for(const ref of visibleChoices().filter(selectable)) {
        if(all.checked)draft.selected.add(ref.report_id);else draft.selected.delete(ref.report_id);
      }
      update();
    };
    search.oninput=()=>{if(draft)draw();};
    find('form').onsubmit=async event=>{
      event.preventDefault();
      if(!draft || submit.disabled)return;
      if(draft.ticket!==getContext().ticket){dialog.close();return;}
      const token=revision, selected=[...draft.selected];draft.saving=true;update();
      find('.reading-reference-error').textContent='';
      try {await save(selected);if(current(token))dialog.close();}
      catch(e){if(current(token))find('.reading-reference-error').textContent=e.message;}
      finally {if(current(token)){draft.saving=false;update();}}
    };
    return {
      async open() {
        const token=++revision, context=getContext();
        const references=context.references.slice();
        draft={ticket:context.ticket,catalog:[],initial:new Set(references.map(ref=>ref.report_id)),
          selected:new Set(references.map(ref=>ref.report_id)),loading:true,saving:false,loadError:false};
        search.value='';find('.reading-reference-error').textContent='';
        draw();dialog.showModal();
        try {
          const reports=await loadCatalog();
          if(!current(token))return;
          if(draft.ticket!==getContext().ticket){dialog.close();return;}
          const pinned=new Set(references.map(ref=>ref.paper.arxiv_id));
          draft.catalog=[...references,...reports.filter(ref=>!pinned.has(ref.paper.arxiv_id))];
        }catch(e){if(current(token)){draft.loadError=true;find('.reading-reference-error').textContent=e.message;}}
        finally {if(current(token)){draft.loading=false;draw();}}
      },
    };
  }

  // History owns result ordering and selection, and reports conversation actions outward.
  function createConversationHistory({host, view, reportHistory, api, getContext, onOpen, onDeleted, onRenamed, error}) {
    const $=selector=>host.querySelector(selector), history=$('.reading-history');
    let historyRequest = 0, historyScope = 'current', historyLoading = false;
    let batchMode = false, deletingHistory = false, visibleHistory = [];
    const selectedHistory = new Set();
    history.querySelector('.reading-history-filters').insertAdjacentHTML('afterend', '<div class="reading-history-batch"><button type="button" data-action="batch-history">批量删除</button><div class="reading-history-selection" hidden><label><input type="checkbox" data-action="select-history">全选</label><span class="reading-history-selected-count"></span><button type="button" data-action="delete-history">删除所选</button></div><p class="reading-history-batch-status" role="status"></p></div>');
    function updateHistorySelection() {
      const {currentAid}=getContext();
      history.classList.toggle('is-selecting', batchMode);
      $('[data-action=batch-history]').textContent = batchMode ? '取消' : '批量删除';
      $('[data-action=batch-history]').disabled = deletingHistory;
      $('.reading-history-selection').hidden = !batchMode;
      $('.reading-history-selected-count').textContent = `已选 ${selectedHistory.size} 项`;
      const all = $('[data-action=select-history]');
      all.checked = visibleHistory.length > 0 && visibleHistory.every(row=>selectedHistory.has(row.id));
      all.indeterminate = selectedHistory.size > 0 && !all.checked;
      all.disabled = deletingHistory || historyLoading || !visibleHistory.length;
      $('[data-action=delete-history]').disabled = deletingHistory || historyLoading || !selectedHistory.size;
      history.querySelectorAll('.reading-history-check').forEach(check=>{check.checked=selectedHistory.has(check.value);check.disabled=deletingHistory || historyLoading;});
      history.querySelectorAll('input[type=search],[data-scope]').forEach(el=>{el.disabled=deletingHistory || (el.dataset.scope==='current' && !currentAid);});
      history.querySelectorAll('.reading-history-actions button,.reading-history-open').forEach(el=>{el.disabled=deletingHistory || historyLoading;});
    }
    $('[data-action=batch-history]').onclick = () => {batchMode=!batchMode;selectedHistory.clear();$('.reading-history-batch-status').textContent='';updateHistorySelection();};
    $('[data-action=select-history]').onchange = event => {selectedHistory.clear();if(event.target.checked)visibleHistory.forEach(row=>selectedHistory.add(row.id));updateHistorySelection();};
    $('[data-action=delete-history]').onclick = async () => {
      const ids = [...selectedHistory];
      if(deletingHistory || historyLoading || !ids.length || !confirm(`确定删除所选 ${ids.length} 个会话及其专属图片？正在生成的回答会停止，此操作无法撤销。`))return;
      deletingHistory=true;updateHistorySelection();
      let deleted=0, failure='';
      $('.reading-history-batch-status').textContent='正在删除…';
      for(const id of ids) {
        try {await api('/sessions/'+id,'DELETE');selectedHistory.delete(id);deleted++;onDeleted(id);}
        catch(e){failure=e.message;}
      }
      deletingHistory=false;
      if(!selectedHistory.size)batchMode=false;
      try {await listHistory();}catch(e){failure=e.message;}
      updateHistorySelection();
      $('.reading-history-batch-status').textContent=`已删除 ${deleted} 个会话。`+(failure ? `部分操作失败：${failure}。可重试未删除的会话。` : '');
    };

    async function listHistory() {
      const {session,currentAid}=getContext();
      const request = ++historyRequest;
      historyLoading=true;updateHistorySelection();
      const aid = reportHistory && historyScope === 'current' ? currentAid : '';
      const q = history.querySelector('input[type=search]').value;
      history.querySelectorAll('[data-scope]').forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.scope === historyScope));
        button.disabled = button.dataset.scope === 'current' && !currentAid;
      });
      const context = $('.reading-history-context');
      if (context) {context.textContent = session?.paper?.title || currentAid; context.title = context.textContent;}

      let rows;
      try {rows=await api('/sessions?' + new URLSearchParams({arxiv_id: aid, q, mode:reportHistory ? 'report' : ''}));}
      catch(e){
        if(request===historyRequest){historyLoading=false;visibleHistory=[];selectedHistory.clear();$('.reading-history-list').replaceChildren();updateHistorySelection();}
        throw e;
      }
      if(request !== historyRequest)return;
      historyLoading=false;
      const activeId=getContext().session?.id;
      view.renderHistorySummary(historyScope, rows.length, q);
      visibleHistory = rows;
      const available = new Set(rows.map(row=>row.id));
      for(const id of selectedHistory)if(!available.has(id))selectedHistory.delete(id);
      const list = $('.reading-history-list'); list.replaceChildren();
      for (const row of rows) {
        const item = textNode('div', '', 'reading-history-row');
        item.classList.toggle('selected', row.id === activeId);
        const {button,rename,remove}=view.historyCard(row);
        if(row.id===activeId)button.setAttribute('aria-current','true');
        button.onclick=()=>onOpen(row).catch(error);
        const actions = textNode('div', '', 'reading-history-actions');
        rename.onclick = () => renameSession(row); remove.onclick = () => deleteSession(row);
        const check = document.createElement('input');check.type='checkbox';check.className='reading-history-check';check.value=row.id;
        check.setAttribute('aria-label','选择会话：'+row.title);
        check.onchange=()=>{if(check.checked)selectedHistory.add(row.id);else selectedHistory.delete(row.id);updateHistorySelection();};
        actions.append(rename, remove); item.append(check, button, actions); list.append(item);
      }
      updateHistorySelection();
      if(!rows.length) list.append(textNode('p', q.trim() ? '没有找到匹配的会话，试试其他关键词。' : '还没有会话，返回对话开始提问吧。', 'reading-history-empty'));
    }
    history.querySelectorAll('[data-scope]').forEach(button => {
      button.onclick = () => {selectedHistory.clear(); historyScope = button.dataset.scope; listHistory().catch(error);};
    });
    history.querySelector('input[type=search]').oninput = () => {selectedHistory.clear();listHistory().catch(error);};
    async function renameSession(row) {
      const title=prompt('会话标题',row.title);
      if(!title?.trim())return;
      try {await api('/sessions/'+row.id,'PATCH',{title:title.trim()});await onRenamed(row.id);await listHistory();}catch(e){error(e);}
    }
    async function deleteSession(row) {
      if(!confirm('删除会话“'+row.title+'”及其专属图片？正在生成的回答也会停止。'))return;
      try {
        await api('/sessions/'+row.id,'DELETE');
        onDeleted(row.id);
        await listHistory();
      }catch(e){error(e);}
    }
    return {refresh:listHistory, rename:renameSession, remove:deleteSession,
      showAll(){historyScope='all';selectedHistory.clear();}};
  }

  // Reading view module: presentation varies here; session execution stays in the caller.
  // Fixed application markup only. Paper titles and message text use textContent.
  function createReadingView(host, mode) {
    const adapters = {
      report: {
        menu: `<details class="reading-menu"><summary aria-label="会话操作" title="会话操作">•••</summary><div><button type="button" data-action="rename">重命名会话</button><button type="button" data-action="delete">删除会话</button></div></details>`,
        paper: `<div class="reading-paper" title="当前论文">选择一篇论文，开始阅读</div>`,
        scope: `<div class="reading-history-scope" role="group" aria-label="历史范围"><button type="button" data-scope="all" aria-pressed="false">全部</button><button type="button" data-scope="current" aria-pressed="true">当前论文</button><span class="reading-history-context"></span></div>`,
        summary: `<div class="reading-history-summary" aria-live="polite"></div>`,
        historyHeading: `<strong>历史记录</strong><button type="button" data-action="close-history">返回对话</button>`,
        historyFooter: ``,
        composeTools: `<div class="reading-suggestions"><button type="button" data-prompt="请查阅原文，按研究问题、核心方法、实验结果和局限总结这篇论文，并给出原文依据。">总结论文</button><button type="button" data-prompt="请查阅论文的方法与实验部分，梳理最值得关注的内容，并解释关键概念，提供原文依据。">论文重点</button><button type="button" data-action="explain">解释选区</button></div>`,
        groundedTitle: `按需查阅当前论文原文`,
        dialog: ``,
        welcomeTitle: `从一个问题，读懂这篇论文`,
      },
      workspace: {
        menu: ``,
        paper: ``,
        scope: ``,
        summary: ``,
        historyHeading: `<strong>会话记录</strong><button type="button" data-action="close-history" aria-label="收起历史记录">×</button>`,
        historyFooter: `<button type="button" data-action="new-workspace" class="reading-new-workspace">＋ 新阅读对话</button>`,
        composeTools: `<div class="reading-compose-tools"><div class="reading-suggestions"><button type="button" data-prompt="请分别总结本轮引用论文的研究问题、方法和结论，注明各篇来源。">总结所选论文</button><button type="button" data-prompt="请比较所选论文的研究问题、方法、实验设置和局限，区分可比与不可直接比较的结果，并注明各篇证据。">交叉对比</button></div><section class="reading-reference-bar" aria-label="引用论文"><strong class="reading-reference-count">引用 · 0</strong><div class="reading-reference-rail"><button type="button" class="reading-reference-nav" data-reference-step="-1" aria-label="查看前面的论文" title="查看前面的论文">‹</button><div class="reading-reference-chips" tabindex="0" aria-label="已引用论文，可横向滚动查看"></div><button type="button" class="reading-reference-nav" data-reference-step="1" aria-label="查看后面的论文" title="查看后面的论文">›</button></div><button type="button" data-action="add-references" title="从报告库添加论文">添加论文</button></section></div>`,
        groundedTitle: `按需查阅本轮引用论文原文`,
        dialog: `<dialog class="reading-reference-dialog"><form><div class="reading-history-heading"><strong>从报告库引用论文</strong><button type="button" data-action="close-references" aria-label="关闭论文选择">×</button></div><input type="search" aria-label="搜索报告论文" placeholder="搜索标题或 arXiv ID"><label class="reading-reference-select-all"><input type="checkbox" data-action="select-all-references" disabled><span>全选当前结果</span></label><div class="reading-reference-options"></div><p class="reading-reference-error" role="status"></p><footer><span class="reading-reference-selection-count"></span><button type="submit" class="reading-primary">加入所选</button></footer></form></dialog>`,
        welcomeTitle: `选几篇论文，一起深入读`,
      },
    };
    const adapter=adapters[mode], isReport=mode==='report';
    host.classList.add('reading-chat');
    host.innerHTML = `<header class="reading-header"><div class="reading-brand"><span class="reading-brand-icon" aria-hidden="true">✦</span><strong>论文助手</strong><span class="reading-header-note">和论文深入聊聊</span></div>
    <div class="reading-toolbar"><button type="button" data-action="history" title="查看会话历史">历史记录</button><button type="button" data-action="settings">设置</button>${adapter.menu}</div></header>
    ${adapter.paper}
    <section class="reading-history" hidden aria-label="历史记录"><div class="reading-history-heading">${adapter.historyHeading}</div><div class="reading-history-filters">${adapter.scope}<input type="search" placeholder="搜索会话、论文或对话内容…" aria-label="搜索会话标题" title="支持会话标题、论文标题、arXiv ID、提问与回答；空格分隔多个关键词"></div>${adapter.summary}<div class="reading-history-list"></div>${adapter.historyFooter}</section>
    <dialog class="reading-settings-dialog"><div class="reading-history-heading"><strong>阅读模型设置</strong><button type="button" data-action="close-settings" aria-label="关闭设置">×</button></div><form class="reading-settings">
    <label><input type="checkbox" name="independent">独立配置阅读模型</label>
    <label>模型<input name="model" maxlength="200"></label><label>服务地址<input name="base_url" placeholder="默认官方接口"></label>
    <label>API Key<input type="password" name="api_key" autocomplete="new-password" placeholder="留空保留已有密钥"></label>
    <label><input type="checkbox" name="clear_key">清除独立密钥</label><label><input type="checkbox" name="images">模型支持图片</label>
    <label>思考强度<select name="reasoning_effort"><option value="high">High · 高</option><option value="medium">Medium · 中</option><option value="low">Low · 低</option><option value="">接口默认（不发送参数）</option></select></label><label>每轮查阅上限<input type="number" name="max_tools" min="1" max="30"></label><label>输出上限<input type="number" name="max_tokens" min="256" max="32000"></label><button class="reading-primary">保存设置</button></form></dialog>
    <p class="reading-status" role="status"></p><div class="reading-messages" aria-live="polite"></div>
    <form class="reading-compose">${adapter.composeTools}<div class="reading-input-box"><div class="reading-selection" hidden><span></span><button type="button" title="移除选区" aria-label="移除选区">×</button></div>
    <div class="reading-previews"></div><textarea aria-label="向论文提问" placeholder="向论文提问，也可以粘贴图表截图…" maxlength="16000" rows="2"></textarea>
    <div class="reading-input-actions"><span>Enter 发送 · Shift + Enter 换行</span><button type="button" data-action="retry" hidden>重试回答</button><button type="button" data-action="stop" hidden>■ 停止</button><button type="submit" class="reading-send" title="发送问题" aria-label="发送问题">↑</button></div></div>
    <div class="reading-footer"><button type="button" data-action="new">＋ 新对话</button><button type="button" data-action="attach" title="附加图片，也可直接粘贴截图">＋ 图片</button><input type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><button type="button" class="reading-model-label" data-action="model" title="阅读模型设置">加载模型…</button><span class="reading-grounded" title="${adapter.groundedTitle}">原文查阅</span></div></form>${adapter.dialog}`;
    const find=selector=>host.querySelector(selector);
    return {
      renderPaper(value) {
        if(!isReport)return;
        const title=value.paper.title+' · v'+value.paper.version;
        find('.reading-paper').textContent=title;find('.reading-paper').title=title;
      },
      setSessionActions(enabled) {
        if(!isReport)return;
        find('[data-action=rename]').disabled=find('[data-action=delete]').disabled=!enabled;
      },
      bindSessionActions(rename, remove) {
        if(!isReport)return;
        find('[data-action=rename]').onclick=rename;find('[data-action=delete]').onclick=remove;
      },
      showWelcome(onPrompt) {
        const empty=textNode('div','','reading-welcome');
        empty.append(textNode('div','✦','reading-welcome-icon'),textNode('h2',adapter.welcomeTitle),textNode('p','解释概念、拆解方法、核实实验结论。\n回答会按需查阅原文，并保留可追溯的依据。'));
        const tips=textNode('div','','reading-welcome-tips');
        const prompts=isReport ? ['这篇论文解决了什么问题？','核心方法为什么有效？','实验结论有哪些局限？'] : ['这些论文分别解决了什么问题？','比较它们的方法与实验设置','哪些结论一致，哪些存在分歧？'];
        for(const prompt of prompts){const button=textNode('button',prompt+' ↗');button.type='button';button.onclick=()=>onPrompt(prompt);tips.append(button);}
        empty.append(tips);find('.reading-messages').append(empty);
      },
      renderHistorySummary(scope, count, query) {
        if(isReport)find('.reading-history-summary').textContent=`${scope==='current' ? '当前论文' : '全部论文'} · ${count} 个会话${query.trim() ? ' · 搜索结果' : ''}`;
      },
      historyCard(row) {
        const button=textNode('button','','reading-history-open');button.type='button';
        button.append(textNode('strong',row.title===row.paper.title ? '论文对话' : row.title));
        const refs=row.references || (row.paper?.arxiv_id ? [{paper:row.paper}] : []);
        const subtitle=isReport ? row.paper.title || row.paper.arxiv_id : `${refs.length} 篇 · `+refs.map(r=>r.paper.title).join('、');
        button.append(textNode('span',subtitle,'reading-history-paper-title'));
        const date=isReport ? new Date(row.updated_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : new Date(row.updated_at).toLocaleString([],{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
        button.append(textNode('span',`${isReport ? 'v'+row.paper.version+' · ' : ''}${date}${isReport && Number.isInteger(row.message_count) ? ` · ${row.message_count} 条消息` : ''}`,'reading-history-meta'));
        if(row.search_match)button.append(textNode('span',row.search_match.label+'：'+row.search_match.snippet,'reading-history-match'));
        button.title=row.title+(isReport ? ' · '+row.paper.title : '');
        if(row.archived)button.append(textNode('em','归档 · 只读','reading-history-archive'));
        const rename=isReport ? textNode('button','重命名') : iconButton('重命名会话','edit');
        const remove=isReport ? textNode('button','删除') : iconButton('删除会话','trash');
        rename.type=remove.type='button';remove.classList.add('reading-history-delete');
        rename.setAttribute('aria-label','重命名会话：'+row.title);remove.setAttribute('aria-label','删除会话：'+row.title);
        return {button,rename,remove};
      },
    };
  }
})();
