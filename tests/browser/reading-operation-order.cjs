// A late result in one conversation must never unlock an operation in another.
const {chromium}=require('playwright');
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});try {
 const page=await browser.newPage({viewport:{width:1280,height:900}}),pending={},ready={};
 const paper={arxiv_id:'2609.29718',version:2,title:'Fixture'};
 const sessions=Object.fromEntries(['a','b'].map(id=>[id,{id,paper:{},mode:'workspace',title:'Conversation '+id,updated_at:'2026-10-08T00:00:00Z',messages:[],references:[{report_id:'report',paper}]}]));
 await page.route('http://localhost/**',route=>{
  const p=new URL(route.request().url()).pathname;
  if(p==='/reading-chat.js')return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(process.env.READING_SCRIPT || 'src/paperloom/static/reading-chat.js','utf8')});
  if(p.includes('/api/reading/')){
   if(p.endsWith('/events'))return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
   if(p.endsWith('/settings'))return route.fulfill({json:{model:'fixture'}});
   if(p.endsWith('/sessions'))return route.fulfill({json:route.request().method()==='GET'?Object.values(sessions).filter(s=>!s.empty):sessions.a});
   const id=p.split('/')[4];
   if(['/stop','/rollback'].some(suffix=>p.endsWith(suffix))){pending.action=route;ready.action?.();return;}
   if(p.endsWith('/messages')){pending[id]=route;ready[id]?.();return;}
   return route.fulfill({json:sessions[id]});
  }
  return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="reading-panel"></div><script src="/reading-chat.js"></script>'});
 });
 await page.goto('http://localhost/');
 const send=page.getByRole('button',{name:'发送问题',exact:true}),input=page.getByRole('textbox',{name:'向论文提问'});
 await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
 await input.fill('Question A');const sentA=new Promise(resolve=>ready.a=resolve);await send.click();await sentA;
 await page.locator('.reading-history-open').filter({hasText:'Conversation b'}).click();
 await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
 await input.fill('Question B');const sentB=new Promise(resolve=>ready.b=resolve);await send.click();await sentB;
 const settled=page.waitForResponse(r=>r.url().endsWith('/a/messages'));
 await pending.a.fulfill({status:503,json:{detail:'Old request failed'}});await settled;
 // Let the network callback and its finally run, then inspect the user-facing state.
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.equal(await send.isDisabled(),true,'A completion must not unlock B while B request is pending');
 assert.equal(await input.inputValue(),'Question B');
 await pending.b.fulfill({status:503,json:{detail:'B can retry'}});
 await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
 console.log('PASS stale conversation operation cannot unlock the current send');
 const historyRow=id=>page.locator('.reading-history-open').filter({hasText:'Conversation '+id});
 async function open(id) {
   const loaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/reading/sessions/'+id);
   await historyRow(id).click();await loaded;
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 }
 const assistant={id:'answer',role:'assistant',text:'answer',html:'answer',status:'running'};
 sessions.a.messages=[assistant];
 await open('a');
 let started=new Promise(resolve=>ready.action=resolve);
 await page.locator('[data-action=stop]').click();await started;
 await open('b');
 await pending.action.fulfill({status:503,json:{detail:'late stop failure'}});
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.ok(!(await page.locator('.reading-status').innerText()).includes('late stop failure'));
 assert.equal(await input.inputValue(),'Question B');
 await open('a');
 started=new Promise(resolve=>ready.action=resolve);
 await page.locator('[data-action=stop]').click();await started;
 await open('b');await open('a');
 assistant.status='stopped';
 const reloaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/reading/sessions/a');
 await pending.action.fulfill({json:{}});await reloaded;
 await page.locator('[data-action=stop]').waitFor({state:'hidden'});
 assert.equal(await input.inputValue(),'Question A');
 console.log('PASS returning to A before stop completes refreshes the current server state');
 assistant.status='completed';
 await open('a');
 started=new Promise(resolve=>ready.action=resolve);
 page.once('dialog',d=>d.accept());
 await page.getByRole('button',{name:'回退到本轮提问之前'}).click();await started;
 await open('b');
 const failed=page.waitForResponse(r=>r.url().endsWith('/a/rollback'));
 await pending.action.fulfill({status:503,json:{detail:'late rollback failure'}});await failed;
 assert.equal(await input.inputValue(),'Question B');
 assert.equal(await send.isEnabled(),true);
 assert.ok(!(await page.locator('.reading-status').innerText()).includes('late rollback failure'));
 await open('a');await input.fill('new draft in A');
 started=new Promise(resolve=>ready.action=resolve);
 page.once('dialog',d=>d.accept());
 await page.getByRole('button',{name:'回退到本轮提问之前'}).click();await started;
 await open('b');
 sessions.a.messages=[];sessions.a.empty=true;
 const image='data:image/png;base64,aGVsbG8=';
 await pending.action.fulfill({json:{session:sessions.a,draft:{text:'old question',selection:'excerpt',images:[image],report:{id:'old-report'}}}});
 await page.waitForFunction(()=>!document.querySelector('.reading-history-list').getAttribute('aria-busy') || document.querySelector('.reading-history-list').getAttribute('aria-busy')==='false');
 assert.equal(await input.inputValue(),'Question B');
 await open('a');
 assert.equal(await input.inputValue(),'new draft in A');
 await page.getByRole('button',{name:'恢复回退的问题'}).click();
 assert.equal(await input.inputValue(),'old question');
 assert.equal(await page.locator('.reading-previews img').getAttribute('src'),image);
 assert.equal(await page.locator('.reading-selection span').textContent(),'excerpt');
 await open('b');await open('a');
 assert.equal(await input.inputValue(),'old question');
 assert.equal(await page.locator('.reading-previews img').count(),1);
 const resending=new Promise(resolve=>ready.a=resolve);
 await send.click();await resending;
 const payload=pending.a.request().postDataJSON();
 assert.equal(payload.report_id,'old-report');assert.equal(payload.selection,'excerpt');
 assert.deepEqual(payload.images,[image]);
 await pending.a.fulfill({status:503,json:{detail:'can retry'}});
 await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
 await page.getByRole('button',{name:'丢弃回退草稿'}).click();
 await historyRow('a').waitFor({state:'detached'});
 console.log('PASS stop failure stays in A and first-round rollback restores the selected draft and images');
 sessions.a.empty=false;
 await page.locator('[data-action=history]').click();
 await page.locator('[data-action=history]').click();
 await open('a');
 await input.fill('A sent while leaving');
 let sending=new Promise(resolve=>ready.a=resolve);
 await send.click();await sending;
 await open('b');
 let accepted=page.waitForResponse(r=>r.url().endsWith('/a/messages'));
 await pending.a.fulfill({json:sessions.a});await accepted;
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.equal(await input.inputValue(),'Question B');
 await open('a');
 assert.equal(await input.inputValue(),'','successful send clears the unchanged draft in A');
 await input.fill('A sent before writing a new draft');
 sending=new Promise(resolve=>ready.a=resolve);
 await send.click();await sending;
 await open('b');await open('a');
 await input.fill('A new draft');
 accepted=page.waitForResponse(r=>r.url().endsWith('/a/messages'));
 await pending.a.fulfill({json:sessions.a});await accepted;
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.equal(await input.inputValue(),'A new draft','late success preserves edits made after returning');
 await open('b');await open('a');
 assert.equal(await input.inputValue(),'A new draft');
 console.log('PASS late success clears the sent draft and preserves a replacement draft');
 await page.close();
 await reportDraftScope(browser);
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

async function reportDraftScope(browser) {
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 try {
  const reportA='2026-10-08/reports/a/report.html', reportB='2026-10-08/reports/b/report.html';
  const paper={arxiv_id:'2609.29718',version:2,title:'One paper'};
  const session={id:'shared',paper,mode:'report',title:'Shared conversation',updated_at:'2026-10-08T00:00:00Z',
   messages:[{id:'older-answer',role:'assistant',text:'Older answer',status:'completed'}],
   references:[{report_id:reportB,paper}]};
  let submitted;
  await page.route('http://localhost/**',route=>{
   const request=route.request(), pathname=new URL(request.url()).pathname;
   if(pathname==='/reading-chat.js')return route.fulfill({contentType:'application/javascript',
    body:fs.readFileSync(process.env.READING_SCRIPT || 'src/paperloom/static/reading-chat.js','utf8')});
   if(pathname.endsWith('/settings'))return route.fulfill({json:{model:'fixture'}});
   if(pathname.endsWith('/sessions')){
    if(request.method()==='GET')return route.fulfill({json:[session]});
    const {report_id}=request.postDataJSON();
    session.references=[{report_id,paper}];
    return route.fulfill({json:session});
   }
   if(pathname.endsWith('/rollback')){
    session.messages=[];
    session.updated_at='2026-10-08T00:01:00Z';
    return route.fulfill({json:{session,draft:{text:'Question from report B',selection:'Excerpt from B',report:{id:reportB}}}});
   }
   if(pathname.endsWith('/messages')){
    submitted=request.postDataJSON();
    return route.fulfill({json:session});
   }
   if(pathname.startsWith('/api/'))return route.fulfill({json:session});
   return route.fulfill({contentType:'text/html',body:`<meta charset="utf-8">
    <div class="report-actions"><button id="report-library-add" data-arxiv-id="${paper.arxiv_id}v2" data-report-id="${reportA}">收藏</button></div>
    <aside id="report-papers" hidden></aside><article class="report-article"><p>Excerpt from A</p></article>
    <script src="/reading-chat.js"></script>`});
  });
  await page.goto('http://localhost/');
  const dock=page.locator('.reading-open'), input=page.locator('.reading-compose textarea');
  const send=page.getByRole('button',{name:'发送问题',exact:true}), selection=page.locator('.reading-selection span');
  await dock.click();
  await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
  await input.fill('Question for report A');
  await page.locator('.report-article p').evaluate(node=>{
   const range=document.createRange(), selected=getSelection();
   range.selectNodeContents(node);selected.removeAllRanges();selected.addRange(range);
  });
  await selection.filter({hasText:'Excerpt from A'}).waitFor();
  page.once('dialog',dialog=>dialog.accept());
  await page.getByRole('button',{name:'回退到本轮提问之前'}).click();
  await page.getByRole('button',{name:'恢复回退的问题'}).click();
  assert.equal(await input.inputValue(),'Question from report B');
  assert.equal(await selection.textContent(),'Excerpt from B');
  await dock.click();
  const rebound=page.waitForResponse(response=>response.url().endsWith('/api/reading/sessions')
   && response.request().method()==='POST');
  await dock.click();await rebound;
  await page.waitForFunction(()=>!document.querySelector('.reading-send').disabled);
  assert.equal(await input.inputValue(),'Question for report A');
  assert.equal(await selection.textContent(),'Excerpt from A');
  const response=page.waitForResponse(result=>result.url().endsWith('/shared/messages'));
  await send.click();await response;
  assert.equal(submitted.report_id,reportA);
  assert.equal(submitted.selection,'Excerpt from A');
  console.log('PASS shared-version report session restores and sends the currently open report draft');
 } finally {
  await page.close();
 }
}
