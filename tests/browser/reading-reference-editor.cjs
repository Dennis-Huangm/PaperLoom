// Isolated browser acceptance; no live research data or model calls.
const {chromium}=require('playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
const refs=['2401.12345','2402.12345'].map((aid,i)=>({report_id:'report-'+i,paper:{title:'Paper '+aid,arxiv_id:aid,version:1}}));
let session={id:'fixture',paper:{},mode:'workspace',title:'新阅读对话',references:[],messages:[]};let sent, savedIds;let reportRequest=0,releaseOld,failSave=false;const oldGate=new Promise(resolve=>releaseOld=resolve);
await page.route('http://localhost/**',async r=>{const path=new URL(r.request().url()).pathname;
if(path.endsWith('.css')||path.endsWith('.js'))return r.fulfill({contentType:path.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync('src/arxiv_ra/static/'+path.split('/').pop(),'utf8')});
if(path.includes('/api/reading/')){
if(path.endsWith('/events'))return r.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
if(path.endsWith('/reports')){reportRequest++;if(reportRequest===1)return r.fulfill({status:500,json:{detail:'catalog unavailable'}});if(reportRequest===2){await oldGate;return r.fulfill({json:{reports:[]}});}return r.fulfill({json:{reports:refs}});}
if(path.endsWith('/references')&&failSave)return r.fulfill({status:500,json:{detail:'save unavailable'}});
if(path.endsWith('/references'))savedIds=r.request().postDataJSON().report_ids;
if(path.endsWith('/references'))session={...session,references:r.request().postDataJSON().report_ids.map(id=>session.references.find(x=>x.report_id===id)||refs.find(x=>x.report_id===id))};
if(path.endsWith('/messages')){sent=r.request().postDataJSON();session={...session,messages:[{id:'u',role:'user',text:sent.text,status:'completed',references:session.references},{id:'a',role:'assistant',status:'completed',limited:true,text:'原文第 3 页',html:'<p>结论 <a href="/source.pdf#page=3">原文第 3 页</a></p>',citations:[{id:'c',quote:'Source excerpt',source_note:'fixture',available:true,url:'/source.pdf#page=3',page:3}]}]};}
return r.fulfill({json:path.endsWith('/settings')?{model:'fixture'}:path.endsWith('/sessions')&&r.request().method()==='GET'?(session.archived?[session]:[]):session});}
return r.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="reading-panel" style="height:880px"></div><link rel="stylesheet" href="/reading-chat.css"><script src="/reading-chat.js"></script>'});});
await page.goto('http://localhost/');
const open=page.getByRole('button',{name:'添加论文',exact:true}),close=page.getByRole('button',{name:'关闭论文选择',exact:true});
const all=page.locator('[data-action=select-all-references]'),submit=page.locator('.reading-reference-dialog [type=submit]');
await open.click();await page.getByText('catalog unavailable',{exact:true}).waitFor();assert.equal(await submit.isDisabled(),true);await close.click();
await open.click();await page.getByText('正在加载报告库…',{exact:true}).waitFor();await close.click();await open.click();await all.check();
releaseOld();await page.waitForResponse(r=>r.url().endsWith('/reports'));assert.equal(await page.locator('.reading-reference-option input:checked').count(),2);
failSave=true;await submit.click();await page.getByText('save unavailable',{exact:true}).waitFor();assert.equal(await all.isChecked(),true);assert.equal(await submit.isEnabled(),true);
failSave=false;await submit.click();await page.waitForFunction(()=>document.querySelectorAll('.reading-reference-chip').length===2);
// Catalog upgrades cannot silently replace a pinned reference. Missing references can be removed.
session.references=[{...refs[0],paper:{...refs[0].paper}},{...refs[1],missing:true}];refs[0]={...refs[0],report_id:'upgraded',paper:{...refs[0].paper,version:2}};
await page.reload();await open.click();await page.waitForFunction(()=>document.querySelectorAll('.reading-reference-option').length===2);
assert.equal(await page.locator('.reading-reference-option input').first().getAttribute('value'),'report-0');
assert.equal(await all.isChecked(),true);assert.equal(await page.locator('.reading-reference-option input').nth(1).isEnabled(),true);
await page.locator('.reading-reference-option input').nth(1).uncheck();await submit.click();await page.waitForFunction(()=>document.querySelectorAll('.reading-reference-chip').length===1);
assert.deepEqual(savedIds,['report-0']);
// The saved selection retains the original version and drops only the missing reference.
assert.deepEqual(errors,[]);console.log('PASS catalog failure/reopen, stale response, failed-save retry, pinned and missing references');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
