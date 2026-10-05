// Isolated browser acceptance; no live research data or model calls.
const {chromium}=require('playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
const refs=['2401.12345','2402.12345'].map((aid,i)=>({report_id:'report-'+i,paper:{title:'Paper '+aid,arxiv_id:aid,version:1}}));
let session={id:'fixture',paper:{},mode:'workspace',title:'新阅读对话',references:[],messages:[]};let sent;
await page.route('http://localhost/**',async r=>{const path=new URL(r.request().url()).pathname;
if(path.endsWith('.css')||path.endsWith('.js'))return r.fulfill({contentType:path.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync('src/arxiv_ra/static/'+path.split('/').pop(),'utf8')});
if(path.includes('/api/reading/')){
if(path.endsWith('/events'))return r.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
if(path.endsWith('/reports'))return r.fulfill({json:{reports:refs}});
if(path.endsWith('/references'))session={...session,references:refs.filter(x=>r.request().postDataJSON().report_ids.includes(x.report_id))};
if(path.endsWith('/messages')){sent=r.request().postDataJSON();session={...session,messages:[{id:'u',role:'user',text:sent.text,status:'completed',references:session.references}]};}
return r.fulfill({json:path.endsWith('/settings')?{model:'fixture'}:path.endsWith('/sessions')&&r.request().method()==='GET'?[]:session});}
return r.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="reading-panel" style="height:880px"></div><link rel="stylesheet" href="/reading-chat.css"><script src="/reading-chat.js"></script>'});});
await page.goto('http://localhost/');await page.getByRole('button',{name:'添加论文',exact:true}).click();
await page.getByRole('checkbox').nth(0).check();await page.getByRole('checkbox').nth(1).check();await page.getByRole('button',{name:/加入所选/}).click();
assert.equal(await page.locator('.reading-reference-chip').count(),2);assert.equal(await page.locator('[aria-label="历史范围"]').count(),0);assert.equal(await page.locator('#reading-aid').count(),0);
await page.getByRole('textbox',{name:'向论文提问'}).fill('比较两篇的实验设计');await page.getByRole('button',{name:'发送问题',exact:true}).click();await page.locator('.reading-message.user').waitFor();assert.equal(sent.text,'比较两篇的实验设计');
await page.locator('.reading-reference-chip button').first().click();await page.waitForFunction(()=>document.querySelectorAll('.reading-reference-chip').length===1);assert.equal(await page.locator('.reading-reference-chip').count(),1);
await page.screenshot({path:'work/reading-references-desktop.png'});await page.setViewportSize({width:390,height:844});await page.locator('[data-action=close-history]').click();await page.screenshot({path:'work/reading-references-mobile.png'});
session={...session,archived:true};await page.reload();await page.waitForFunction(()=>document.querySelector('[data-action=add-references]').disabled);page.on('dialog',dialog=>dialog.accept());await page.locator('.reading-menu summary').click();await page.locator('[data-action=delete]').click();await page.waitForFunction(()=>document.querySelectorAll('.reading-reference-chip').length===0);assert.equal(await page.locator('[data-action=add-references]').isEnabled(),true);
assert.deepEqual(errors,[]);console.log('PASS multi-select, scope, send, remove, responsive rendering');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
