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
   if(p.endsWith('/sessions'))return route.fulfill({json:route.request().method()==='GET'?Object.values(sessions):sessions.a});
   const id=p.split('/')[4];
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
 await page.close();
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
