const {chromium}=require('playwright');
const fs=require('fs');const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
const page=await browser.newPage({viewport:{width:1200,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
const paper={title:'PPTBench: Can Coding Agents Reconstruct the Visual World through Structured, Editable Slides',arxiv_id:'2609.29718',version:2};
const rows=Array.from({length:5},(_,i)=>({id:'test'+i,title:i?'你是什么模型？':'论文的实验设定中，给模型的输入是什么',paper,updated_at:'2026-10-04T06:43:00Z',message_count:4,archived:i===2}));
const session={...rows[0],messages:[]};
await page.route('http://fixture/**',r=>{const p=new URL(r.request().url()).pathname;
if(p.endsWith('.css')||p.endsWith('.js'))return r.fulfill({contentType:p.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync('src/arxiv_ra/static/'+p.split('/').pop(),'utf8')});
if(p.includes('/api/reading/')){if(p.endsWith('/events'))return r.fulfill({contentType:'text/event-stream',body:'event: snapshot\ndata: '+JSON.stringify(session)+'\n\n'});return r.fulfill({json:p.endsWith('/settings')?{model:'fixture'}:p.endsWith('/sessions')&&r.request().method()==='GET'?(new URL(r.request().url()).searchParams.get('q')?[]:rows):session});}
return r.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div class="report-actions"><button id="report-library-add" data-arxiv-id="2609.29718v2">收藏</button></div><article class="report-article">报告正文</article><aside id="report-papers" hidden></aside><link rel="stylesheet" href="/reading-chat.css"><script src="/reading-chat.js"></script>'});});
await page.goto('http://fixture/');await page.locator('.reading-open').click();await page.locator('[data-action=history]').click();await page.locator('.reading-history-row').first().waitFor();
assert.equal(await page.locator('#reading-aid,.reading-start,.reading-reference-bar').count(),0);assert.equal(await page.locator('.reading-menu').count(),1);assert.equal(await page.locator('[data-action=explain]').count(),1);assert.equal(await page.locator('.reading-history-row').count(),5);assert.equal(await page.locator('.reading-history-group').count(),0);
for(const width of [480,380,320]){await page.evaluate(w=>document.body.style.setProperty('--reading-width',w+'px'),width);assert.equal(await page.locator('.reading-history-list').evaluate(e=>e.scrollWidth<=e.clientWidth),true);await page.screenshot({path:'work/history-layout-'+width+'.png'});}
await page.locator('[data-scope=all]').click();await page.waitForFunction(()=>document.querySelector('.reading-history-summary').textContent.startsWith('全部论文'));
await page.locator('[aria-label="搜索会话标题"]').fill('不存在');await page.locator('.reading-history-empty').waitFor();await page.locator('[data-action=close-history]').click();assert.equal(await page.locator('.reading-history').isHidden(),true);assert.deepEqual(errors,[]);console.log('PASS flat cards, widths 480/380/320, scope, search, return, no browser errors');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
