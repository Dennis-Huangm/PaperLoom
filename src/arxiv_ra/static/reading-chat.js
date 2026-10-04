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
    document.querySelector('.report-actions').append(open);
    open.onclick = async () => {
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
  host.classList.add('reading-chat');
  // This is fixed application markup. All external text is assigned via textContent.
  host.innerHTML = `<header class="reading-header"><div class="reading-brand"><span class="reading-brand-icon" aria-hidden="true">✦</span><strong>论文助手</strong><span class="reading-header-note">和论文深入聊聊</span></div>
    <div class="reading-toolbar"><button type="button" data-action="history" title="查看会话历史">历史记录</button><button type="button" data-action="settings">设置</button><details class="reading-menu"><summary aria-label="会话操作" title="会话操作">•••</summary><div><button type="button" data-action="rename">重命名会话</button><button type="button" data-action="delete">删除会话</button></div></details></div></header>
    <div class="reading-paper" title="当前论文">选择一篇论文，开始阅读</div>
    <section class="reading-history" hidden><div class="reading-history-heading"><strong>会话记录</strong><button type="button" data-action="close-history" aria-label="收起历史记录">×</button></div><div class="reading-history-filters"><select aria-label="历史范围"><option value="current">当前论文</option><option value="all">全部论文</option></select><input type="search" placeholder="搜索会话…" aria-label="搜索会话标题"></div><div class="reading-history-list"></div><form class="reading-start"><label for="reading-aid">从论文开始</label><div><input id="reading-aid" aria-label="arXiv ID" placeholder="arXiv ID，如 2603.29852v2" required><button title="打开论文" aria-label="打开论文">↗</button></div></form></section>
    <dialog class="reading-settings-dialog"><div class="reading-history-heading"><strong>阅读模型设置</strong><button type="button" data-action="close-settings" aria-label="关闭设置">×</button></div><form class="reading-settings">
    <label><input type="checkbox" name="independent">独立配置阅读模型</label>
    <label>模型<input name="model" maxlength="200"></label><label>服务地址<input name="base_url" placeholder="默认官方接口"></label>
    <label>API Key<input type="password" name="api_key" autocomplete="new-password" placeholder="留空保留已有密钥"></label>
    <label><input type="checkbox" name="clear_key">清除独立密钥</label><label><input type="checkbox" name="images">模型支持图片</label>
    <label>思考强度<select name="reasoning_effort"><option value="high">High · 高</option><option value="medium">Medium · 中</option><option value="low">Low · 低</option><option value="">接口默认（不发送参数）</option></select></label><label>每轮查阅上限<input type="number" name="max_tools" min="1" max="30"></label><label>输出上限<input type="number" name="max_tokens" min="256" max="32000"></label><button class="reading-primary">保存设置</button></form></dialog>
    <p class="reading-status" role="status"></p><div class="reading-messages" aria-live="polite"></div>
    <form class="reading-compose"><div class="reading-suggestions"><button type="button" data-prompt="请查阅原文，按研究问题、核心方法、实验结果和局限总结这篇论文，并给出原文依据。">总结论文</button><button type="button" data-prompt="请查阅论文的方法与实验部分，梳理最值得关注的内容，并解释关键概念，提供原文依据。">论文重点</button><button type="button" data-action="explain">解释选区</button></div><div class="reading-input-box"><div class="reading-selection" hidden><span></span><button type="button" title="移除选区" aria-label="移除选区">×</button></div>
    <div class="reading-previews"></div><textarea aria-label="向论文提问" placeholder="向论文提问，也可以粘贴图表截图…" maxlength="16000" rows="2"></textarea>
    <div class="reading-input-actions"><span>Enter 发送 · Shift + Enter 换行</span><button type="button" data-action="retry" hidden>重试回答</button><button type="button" data-action="stop" hidden>■ 停止</button><button type="submit" class="reading-send" title="发送问题" aria-label="发送问题">↑</button></div></div>
    <div class="reading-footer"><button type="button" data-action="new">＋ 新对话</button><button type="button" data-action="attach" title="附加图片，也可直接粘贴截图">＋ 图片</button><input type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><button type="button" class="reading-model-label" data-action="model" title="阅读模型设置">加载模型…</button><span class="reading-grounded" title="按需查阅当前论文原文">原文查阅</span></div></form>`;
  const $ = selector => host.querySelector(selector);
  if(papers)installDock();
  const input = $('textarea'), status = $('.reading-status'), messages = $('.reading-messages');
  const compose = $('.reading-compose'), settingsForm = $('.reading-settings');
  const selectionBox = $('.reading-selection'), history = $('.reading-history');
  let session = null, selection = '', pictures = [], requestToken = null, busy = false, generation = 0, lastRender = '';
  let currentAid = host.dataset.arxivId || '', currentReport = host.dataset.reportId || '';
  let profile = '';
  let historyRequest = 0;
  const labels = {queued:'等待开始',running:'正在回答',completed:'',failed:'回答未完成',stopped:'已停止',interrupted:'已中断'};
  const expanded = new Set();
  function setHistory(open) {history.hidden = !open; host.classList.toggle('history-open', open); $('[data-action=history]').setAttribute('aria-expanded', String(open)); if(open) listHistory().catch(error);}
  const settingsDialog = $('.reading-settings-dialog');
  $('[data-action=settings]').onclick = $('[data-action=model]').onclick = () => settingsDialog.showModal();
  $('[data-action=close-settings]').onclick = () => settingsDialog.close();
  $('[data-action=close-history]').onclick = () => setHistory(false);
  $('[data-action=attach]').onclick = () => compose.querySelector('[type=file]').click();
  input.addEventListener('keydown', event => {if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); if(!compose.querySelector('[type=submit]').disabled)compose.requestSubmit();}});
  host.querySelectorAll('[data-prompt]').forEach(button => {button.onclick = () => {input.value = button.dataset.prompt; input.oninput(); input.focus();};});
  $('[data-action=explain]').onclick = () => {if(!selection) {status.textContent='先在报告正文中选中文字，再点击“阅读对话 / 提问选区”。';return;} input.value='请结合原文解释这段选区，说明关键概念及其在论文中的作用。'; input.oninput(); input.focus();};
  $('.reading-start').onsubmit = event => {event.preventDefault();saveDraft(); generation++; session=null; currentAid=$('#reading-aid').value.trim(); currentReport=''; selection=''; pictures=[]; input.value='';showSelection();showPictures();start().catch(error);};
  const draftKey = () => 'paperloom.reading.draft:' + (session?.id || currentAid) + ':' + currentReport;
  function saveDraft() {try {sessionStorage.setItem(draftKey(), JSON.stringify({text: input.value, selection, report: currentReport}));} catch { /* optional */ }}
  function restoreDraft() {try {const d = JSON.parse(sessionStorage.getItem(draftKey()) || '{}'); input.value = d.text || ''; selection = d.report === currentReport ? d.selection || '' : ''; showSelection();} catch { /* optional */ }}
  function showSelection() {selectionBox.hidden = !selection; selectionBox.querySelector('span').textContent = selection; $('[data-action=explain]').disabled = !selection;}
  function setSelection(text) {selection = text.slice(0, 20000); showSelection(); saveDraft();}
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
    const paths={copy:'M9 9h11v11H9z M15 5V3H3v12h2',undo:'M9 5 4 10l5 5 M4 10h10a6 6 0 0 1 6 6v3',close:'m6 6 12 12 M6 18 18 6'};
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
    const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]);svg.append(path);button.append(svg);return button;
  }
  function openDock() {
    document.dispatchEvent(new Event('reading-dock-opening'));host.hidden=false;document.body.classList.add('reading-dock-open');
  }
  function installDock() {
    host.classList.add('reading-dock');
    const close=iconButton('收起阅读助手','close');close.onclick=()=>{host.hidden=true;document.body.classList.remove('reading-dock-open');document.querySelector('.reading-open')?.focus();};
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
    document.querySelector('#report-papers-toggle')?.addEventListener('click',()=>{host.hidden=true;document.body.classList.remove('reading-dock-open');});
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
    source.addEventListener('deleted',()=>{source.close();if(current()){session=null;messages.replaceChildren();showWelcome();status.textContent='会话已删除';}});
    source.onerror=()=>{if(current())status.textContent='连接暂时中断，正在恢复实时更新…';};
  }
  function render(value) {
    if(session?.id===value.id && session.updated_at && value.updated_at && value.updated_at < session.updated_at)return;
    session = value;watchSession();
    $('.reading-paper').textContent = value.paper.title + ' · v' + value.paper.version;
    $('.reading-paper').title = value.paper.title + ' · v' + value.paper.version;
    const active = value.messages.some(m => ['queued', 'running'].includes(m.status));
    compose.querySelector('[type=submit]').disabled = active || busy || !!value.archived;
    input.disabled=!!value.archived;
    if(value.archived)status.textContent='回退前归档 · 只读，可从历史记录查看完整内容';
    $('[data-action=stop]').disabled = !active;
    $('[data-action=stop]').hidden = !active;
    compose.querySelector('[type=submit]').hidden = active;
    const last = value.messages.at(-1);
    $('[data-action=retry]').disabled = !last || !['failed', 'stopped', 'interrupted'].includes(last.status);
    $('[data-action=retry]').hidden = $('[data-action=retry]').disabled;
    $('[data-action=rename]').disabled = $('[data-action=delete]').disabled = !session;
    const stamp = JSON.stringify(value.messages);
    status.classList.remove('reading-error');
    status.textContent = value.archived ? '回退前归档 · 只读' : active ? (last?.detail || '正在准备回答…') : '';
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
        thinking.append(textNode('summary','思考过程 · 模型返回'),textNode('div',message.reasoning));
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
      if(message.role === 'assistant' && message.status === 'completed' && message.limited)box.append(textNode('small','本轮已达查阅或输出上限，可以继续追问。'));
      if(message.role === 'assistant' && !message.text && ['running','queued'].includes(message.status))box.append(textNode('div','正在查阅并整理回答…','reading-thinking'));
      if(message.role === 'assistant' && (message.text || !['queued','running'].includes(message.status))) {
        const actions=textNode('div','','reading-answer-actions'); const copy=iconButton('复制回答','copy');
        copy.onclick=()=>navigator.clipboard.writeText(message.text).then(()=>{copy.title='已复制';}).catch(()=>error(new Error('无法访问剪贴板，请手动选择文字复制')));
        const back=iconButton('回退到本轮提问之前','undo');back.disabled=active || !!value.archived;
        back.onclick=async()=>{
          if(!confirm('回退到本轮提问之前？当前完整对话将归档保留，原问题恢复到输入框。'))return;
          const sid=session.id,ticket=generation;
          try{const result=await api('/sessions/'+sid+'/rollback','POST',{message_id:message.id});if(session?.id!==sid || generation!==ticket)return;
            generation++;lastRender='';render(result.session);input.value=result.draft.text;selection=result.draft.selection || '';pictures=result.draft.images || [];currentReport=result.draft.report?.id || '';requestToken=null;showSelection();showPictures();saveDraft();input.focus();if(!history.hidden)await listHistory();
          }catch(e){error(e);}
        };
        actions.append(copy,back,textNode('span',message.model || '')); box.append(actions);
      }
      for (const data of message.images || []) {const img = document.createElement('img'); img.src = data; img.alt = '本条问题图片'; box.append(img);}
      if (message.citations?.length) {
        const details = document.createElement('details'); details.className = 'reading-citations';
        details.append(textNode('summary', `原文依据 · ${message.citations.length} 处`));
        const key=message.id+':sources';details.open=expanded.has(key);details.ontoggle=()=>{if(details.open)expanded.add(key);else expanded.delete(key);};
        for (const c of message.citations) {
          const quote = textNode('blockquote', c.quote + '\n' + c.source_note);
          if (c.available) {const a = textNode('a', `原文第 ${c.page} 页`); a.href = c.url; a.target = '_blank'; a.rel = 'noopener'; quote.append(a);}
          else quote.append(textNode('small', '原文文件已缺失，保留当时摘录'));
          details.append(quote);
        }
        box.append(details);
      }
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
    const empty = textNode('div','','reading-welcome');
    empty.append(textNode('div','✦','reading-welcome-icon'),textNode('h2','从一个问题，读懂这篇论文'),textNode('p','解释概念、拆解方法、核实实验结论。\n回答会按需查阅原文，并保留可追溯的依据。'));
    const tips=textNode('div','','reading-welcome-tips');
    for(const prompt of ['这篇论文解决了什么问题？','核心方法为什么有效？','实验结论有哪些局限？']) {const button=textNode('button',prompt+' ↗');button.type='button';button.onclick=()=>{input.value=prompt;input.oninput();input.focus();};tips.append(button);}
    empty.append(tips);messages.append(empty);
  }
  async function refresh() {
    if (!session) return;
    const sid = session.id, ticket = generation;
    const value = await api('/sessions/' + sid);
    if (ticket === generation && session?.id === sid) render(value);
  }
  async function start(fresh = false) {
    if (!currentAid) {history.querySelector('select').value = 'all'; setHistory(true); messages.replaceChildren();showWelcome();return;}
    busy = true;
    const ticket = ++generation;
    status.textContent = '正在确认论文版本…';
    try {
      const value = await api('/sessions', 'POST', {arxiv_id: currentAid, report_id: currentReport, new: fresh,
        origin: host.dataset.origin || '', source_date: host.dataset.sourceDate || ''});
      if (ticket !== generation) return;
      session = value; await refresh();
      if(!history.hidden)await listHistory();
      // Keep a question drafted before version resolution.
      if (!input.value) restoreDraft();
    } finally {busy = false; if(session) compose.querySelector('[type=submit]').disabled = !!session.archived || !!session.messages?.some(m => ['queued','running'].includes(m.status));}
  }
  async function listHistory() {
    const request = ++historyRequest;
    const aid = history.querySelector('select').value === 'current' ? currentAid : '';
    const q = history.querySelector('input').value;
    const rows = await api('/sessions?' + new URLSearchParams({arxiv_id: aid, q}));
    if(request !== historyRequest)return;
    const list = $('.reading-history-list'); list.replaceChildren();
    for (const row of rows) {
      const button = textNode('button', '', 'reading-history-row');
      button.classList.toggle('selected', row.id === session?.id);
      button.append(textNode('strong',row.title),textNode('span',`v${row.paper.version} · ${new Date(row.updated_at).toLocaleString([], {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}`));button.title=row.title+'\n'+row.paper.title;
      button.onclick = async () => {
        saveDraft(); generation++; session = row;
        currentAid = row.paper.arxiv_id + 'v' + row.paper.version;
        currentReport = ''; selection = ''; pictures = []; showPictures(); input.value = ''; restoreDraft();
        try {lastRender='';await refresh();if(papers || innerWidth < 850)setHistory(false);else await listHistory();} catch(e) {error(e);}
      };
      list.append(button);
    }
    if(!rows.length) list.textContent = '还没有会话';
  }
  history.querySelector('select').onchange = () => listHistory().catch(error);
  history.querySelector('input').oninput = () => listHistory().catch(error);
  $('[data-action=history]').onclick = () => setHistory(history.hidden);
  $('[data-action=new]').onclick = () => {saveDraft(); input.value = ''; selection = ''; pictures = []; showSelection(); showPictures(); start(true).catch(error);};
  $('[data-action=rename]').onclick = async () => {if(!session)return; const title=prompt('会话标题',session.title); if(title)try{await api('/sessions/'+session.id,'PATCH',{title}); await refresh(); await listHistory();}catch(e){error(e);}};
  $('[data-action=delete]').onclick = async () => {
    if(!session || !confirm('删除此会话及其专属图片？正在生成的回答也会停止。'))return;
    try {const sid=session.id;await api('/sessions/'+sid,'DELETE');if(session?.id!==sid)return; generation++; session=null;watchSession(); input.value='';selection='';pictures=[];showSelection();showPictures();messages.replaceChildren();showWelcome(); lastRender=''; status.textContent='会话已删除';setHistory(true);}catch(e){error(e);}
  };
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
      if(!history.hidden)await listHistory();
    } finally {busy=false; compose.querySelector('[type=submit]').disabled=!!session?.archived || !!session?.messages?.some(m=>['queued','running'].includes(m.status));}
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
})();
