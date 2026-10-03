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
    const tabs = document.createElement('div'); tabs.className = 'reading-tabs';
    const listTab = document.createElement('button'); listTab.textContent = '论文列表';
    const chatTab = document.createElement('button'); chatTab.textContent = '阅读对话';
    tabs.append(listTab, chatTab); papers.querySelector('.report-nav-heading').after(tabs);
    host = document.createElement('div'); host.id = 'report-reading'; host.hidden = true;
    Object.assign(host.dataset, reportButton ? reportButton.dataset : papers.dataset);
    papers.append(host);
    listTab.onclick = () => {host.hidden = true; papers.classList.remove('reading-active');};
    chatTab.onclick = () => {host.hidden = false; papers.classList.add('reading-active');};
    const open = document.createElement('button'); open.className = 'reading-open'; open.textContent = '阅读对话 / 提问选区';
    document.querySelector('.report-actions').append(open);
    open.onclick = async () => {
      const selection = window.getSelection();
      const article = document.querySelector('.report-article');
      const text = selection && article.contains(selection.anchorNode) && article.contains(selection.focusNode) ? selection.toString() : '';
      if (papers.hidden) document.querySelector('#report-papers-toggle').click();
      chatTab.click();
      try {
        // The report remains the source even after browsing another paper's history.
        if (currentReport !== host.dataset.reportId) {
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
  host.innerHTML = `<div class="reading-paper"></div>
    <div class="reading-toolbar"><button type="button" data-action="history">历史记录</button><button type="button" data-action="new">新对话</button><button type="button" data-action="rename">重命名</button><button type="button" data-action="delete">删除</button></div>
    <section class="reading-history" hidden><div class="reading-toolbar"><select aria-label="历史范围"><option value="current">当前论文</option><option value="all">全部论文</option></select><input type="search" placeholder="搜索会话标题" aria-label="搜索会话标题"></div><div class="reading-history-list"></div></section>
    <details><summary>阅读模型设置</summary><form class="reading-settings">
    <label><input type="checkbox" name="independent">独立配置阅读模型</label>
    <label>模型<input name="model" maxlength="200"></label><label>服务地址<input name="base_url" placeholder="默认官方接口"></label>
    <label>API Key<input type="password" name="api_key" autocomplete="new-password" placeholder="留空保留已有密钥"></label>
    <label><input type="checkbox" name="clear_key">清除独立密钥</label><label><input type="checkbox" name="images">模型支持图片</label>
    <label>每轮查阅上限<input type="number" name="max_tools" min="1" max="30"></label><label>输出上限<input type="number" name="max_tokens" min="256" max="32000"></label><button>保存设置</button></form></details>
    <p class="reading-status" role="status"></p><div class="reading-messages" aria-live="polite"></div>
    <form class="reading-compose"><div class="reading-selection" hidden><span></span><button type="button">移除选区</button></div>
    <div class="reading-previews"></div><textarea aria-label="向论文提问" placeholder="哪里没看懂？可以继续追问，也可以粘贴截图。" maxlength="16000"></textarea>
    <div class="reading-toolbar"><label>图片<input type="file" accept="image/png,image/jpeg,image/webp" multiple></label><button type="submit">发送</button><button type="button" data-action="stop">停止</button><button type="button" data-action="retry">重试回答</button></div></form>`;
  const $ = selector => host.querySelector(selector);
  const input = $('textarea'), status = $('.reading-status'), messages = $('.reading-messages');
  const compose = $('.reading-compose'), settingsForm = $('.reading-settings');
  const selectionBox = $('.reading-selection'), history = $('.reading-history');
  let session = null, selection = '', pictures = [], requestToken = null, busy = false, generation = 0, lastRender = '';
  let currentAid = host.dataset.arxivId || '', currentReport = host.dataset.reportId || '';
  let profile = '';
  const draftKey = () => 'paperloom.reading.draft:' + (session?.id || currentAid) + ':' + currentReport;
  function saveDraft() {try {sessionStorage.setItem(draftKey(), JSON.stringify({text: input.value, selection, report: currentReport}));} catch { /* optional */ }}
  function restoreDraft() {try {const d = JSON.parse(sessionStorage.getItem(draftKey()) || '{}'); input.value = d.text || ''; selection = d.report === currentReport ? d.selection || '' : ''; showSelection();} catch { /* optional */ }}
  function showSelection() {selectionBox.hidden = !selection; selectionBox.querySelector('span').textContent = selection;}
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
  function render(value) {
    session = value;
    $('.reading-paper').textContent = value.paper.title + ' · v' + value.paper.version;
    const active = value.messages.some(m => ['queued', 'running'].includes(m.status));
    compose.querySelector('[type=submit]').disabled = active || busy;
    $('[data-action=stop]').disabled = !active;
    const last = value.messages.at(-1);
    $('[data-action=retry]').disabled = !last || !['failed', 'stopped', 'interrupted'].includes(last.status);
    const stamp = JSON.stringify(value.messages);
    if (stamp === lastRender) return;
    status.classList.remove('reading-error');
    status.textContent = (last?.detail || '可以开始提问') + (last?.summarized ? ' · 较早讨论已使用摘录摘要' : '');
    if (last?.material) status.textContent += ` · 已读取 ${last.material.read_pages?.length || 0}/${last.material.total_pages || '?'} 页 · ${last.material.source_note || ''}`;
    lastRender = stamp;
    const nearEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    messages.replaceChildren();
    for (const message of value.messages) {
      const box = textNode('div', '', 'reading-message ' + message.role);
      box.append(textNode('small', message.role === 'user' ? '你' : `${message.model} · ${message.status}`));
      if (message.selection) box.append(textNode('blockquote', message.selection + (message.report?.missing ? '（来源报告已删除）' : '')));
      const body = document.createElement('div');
      if (message.role === 'assistant' && message.html) body.innerHTML = message.html; // server-sanitized Markdown only
      else body.textContent = message.text;
      box.append(body);
      for (const data of message.images || []) {const img = document.createElement('img'); img.src = data; img.alt = '本条问题图片'; box.append(img);}
      if (message.citations?.length) {
        const details = document.createElement('details'); details.className = 'reading-citations';
        details.append(textNode('summary', '查看原文摘录'));
        for (const c of message.citations) {
          const quote = textNode('blockquote', c.quote + '\n' + c.source_note);
          if (c.available) {const a = textNode('a', `原文第 ${c.page} 页`); a.href = c.url; a.target = '_blank'; a.rel = 'noopener'; quote.append(a);}
          else quote.append(textNode('small', '原文文件已缺失，保留当时摘录'));
          details.append(quote);
        }
        box.append(details);
      }
      messages.append(box);
    }
    if (window.katex) messages.querySelectorAll('.math-inline,.math-block').forEach(node => {
      try {katex.render(node.textContent, node, {displayMode: node.dataset.display === 'true', throwOnError: false, trust: false});} catch { /* visible source */ }
    });
    if (nearEnd) messages.scrollTop = messages.scrollHeight;
  }
  async function refresh() {
    if (!session) return;
    const sid = session.id, ticket = generation;
    const value = await api('/sessions/' + sid);
    if (ticket === generation && session?.id === sid) render(value);
  }
  async function start(fresh = false) {
    if (!currentAid) {history.hidden = false; history.querySelector('select').value = 'all'; await listHistory(); return;}
    busy = true;
    const ticket = ++generation;
    status.textContent = '正在确认论文版本…';
    try {
      const value = await api('/sessions', 'POST', {arxiv_id: currentAid, report_id: currentReport, new: fresh,
        origin: host.dataset.origin || '', source_date: host.dataset.sourceDate || ''});
      if (ticket !== generation) return;
      session = value; await refresh();
      // Keep a question drafted before version resolution.
      if (!input.value) restoreDraft();
    } finally {busy = false; if(session) compose.querySelector('[type=submit]').disabled = session.messages.some(m => ['queued','running'].includes(m.status));}
  }
  async function listHistory() {
    const aid = history.querySelector('select').value === 'current' ? currentAid : '';
    const q = history.querySelector('input').value;
    const rows = await api('/sessions?' + new URLSearchParams({arxiv_id: aid, q}));
    const list = $('.reading-history-list'); list.replaceChildren();
    for (const row of rows) {
      const button = textNode('button', `${row.title} · v${row.paper.version} · ${new Date(row.updated_at).toLocaleString()}`);
      button.onclick = async () => {
        saveDraft(); generation++; session = row;
        currentAid = row.paper.arxiv_id + 'v' + row.paper.version;
        currentReport = ''; selection = ''; pictures = []; showPictures(); input.value = ''; restoreDraft();
        try {await refresh();} catch(e) {error(e);}
      };
      list.append(button);
    }
    if(!rows.length) list.textContent = '还没有会话';
  }
  history.querySelector('select').onchange = () => listHistory().catch(error);
  history.querySelector('input').oninput = () => listHistory().catch(error);
  $('[data-action=history]').onclick = () => {history.hidden = !history.hidden; if(!history.hidden) listHistory().catch(error);};
  $('[data-action=new]').onclick = () => {saveDraft(); input.value = ''; selection = ''; pictures = []; showSelection(); showPictures(); start(true).catch(error);};
  $('[data-action=rename]').onclick = async () => {if(!session)return; const title=prompt('会话标题',session.title); if(title)try{await api('/sessions/'+session.id,'PATCH',{title}); await refresh(); await listHistory();}catch(e){error(e);}};
  $('[data-action=delete]').onclick = async () => {
    if(!session || !confirm('删除此会话及其专属图片？正在生成的回答也会停止。'))return;
    try {await api('/sessions/'+session.id,'DELETE'); generation++; session=null; messages.replaceChildren(); lastRender=''; status.textContent='会话已删除'; await listHistory();}catch(e){error(e);}
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
    } finally {busy=false; compose.querySelector('[type=submit]').disabled=session?.messages.some(m=>['queued','running'].includes(m.status));}
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
    try{await api('/settings','PUT',body);settingsForm.elements.api_key.value='';status.textContent='阅读设置已保存，应用于下一轮回答';}catch(e){error(e);}
  };
  restoreDraft();
  api('/settings').then(value=>{
    profile=value.profile_id;
    for(const [key,item] of Object.entries(value)){const field=settingsForm.elements.namedItem(key);if(field){if(field.type==='checkbox')field.checked=item;else field.value=item;}}
    return start();
  }).catch(error);
  setInterval(()=>{if(session && !busy)refresh().catch(error);},1000);
})();
