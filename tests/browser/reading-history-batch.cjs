// Isolated history management checks; never deletes real conversations.
const {chromium}=require('playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
let rows=['alpha','beta','gamma'].map(id=>({id,title:id,paper:{title:'Paper',arxiv_id:'2401.12345',version:1},references:[],messages:[{id:'u',role:'user',text:'hello',status:'completed'}],updated_at:'2026-10-05T00:00:00Z'}));
let removed=[],failBeta=true,accept=false,releaseSearch;const searchGate=new Promise(resolve=>releaseSearch=resolve);
page.on('dialog',d=>accept?d.accept():d.dismiss());
await page.route('http://localhost/**',async route=>{
const req=route.request(),url=new URL(req.url()),path=url.pathname;
if(path.endsWith('.css')||path.endsWith('.js'))return route.fulfill({contentType:path.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync('src/arxiv_ra/static/'+path.split('/').pop(),'utf8')});
if(path.includes('/api/reading/')){
if(path.endsWith('/events'))return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
if(req.method()==='DELETE'){const id=path.split('/').pop();if(id==='beta'&&failBeta)return route.fulfill({status:500,json:{detail:'fixture failure'}});removed.push(id);rows=rows.filter(r=>r.id!==id);return route.fulfill({json:{deleted:true}});}
if(path.endsWith('/sessions')&&req.method()==='GET'){if(url.searchParams.get('q')==='match')await searchGate;return route.fulfill({json:rows.filter(r=>r.title.includes(url.searchParams.get('q')||''))});}
return route.fulfill({json:path.endsWith('/settings')?{model:'fixture'}:rows.find(r=>path.endsWith('/'+r.id))||{id:'draft',paper:{},references:[],messages:[]}});
}
return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="reading-panel" style="height:880px"></div><link rel="stylesheet" href="/reading-chat.css"><script src="/reading-chat.js"></script>'});});
await page.goto('http://localhost/');await page.locator('.reading-history-open').first().waitFor();
await page.locator('.reading-history-open').first().click();
await page.getByRole('button',{name:'批量删除',exact:true}).click();
await page.locator('[data-action=select-history]').check();assert.equal(await page.locator('.reading-history-check:checked').count(),3);
await page.getByRole('button',{name:'删除所选',exact:true}).click();assert.deepEqual(removed,[]);
accept=true;await page.getByRole('button',{name:'删除所选',exact:true}).click();
await page.waitForFunction(()=>document.querySelector('.reading-history-batch-status').textContent.includes('部分操作失败'));
assert.deepEqual(removed,['alpha','gamma']);assert.equal(await page.locator('.reading-message.user').count(),0);assert.equal(await page.locator('.reading-history-check:checked').count(),1);
failBeta=false;await page.getByRole('button',{name:'删除所选',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.reading-history-batch-status').textContent==='已删除 1 个会话。');assert.equal(rows.length,0);
rows=['keep','match'].map(id=>({id,title:id,paper:{title:'Paper'},references:[],messages:[],updated_at:'2026-10-05T00:00:00Z'}));
await page.getByRole('button',{name:'批量删除',exact:true}).click();await page.getByRole('searchbox',{name:'搜索会话标题'}).fill('match');assert.equal(await page.locator('[data-action=select-history]').isDisabled(),true);assert.equal(await page.locator('[data-action=delete-history]').isDisabled(),true);releaseSearch();await page.waitForFunction(()=>document.querySelector('.reading-history-open strong')?.textContent==='match');
await page.locator('[data-action=select-history]').check();await page.getByRole('button',{name:'删除所选',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.reading-history-open'));assert.deepEqual(rows.map(r=>r.id),['keep']);
assert.deepEqual(errors,[]);console.log('PASS selection, cancellation, partial failure/retry, active conversation cleanup, filtered select-all');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
