// Run against a local PaperLoom server; API calls are isolated fixtures.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
let submitted;
const session={id:'selection-test',title:'Test',paper:{title:'Test paper',version:1,arxiv_id:'2401.12345'},messages:[]};
await page.route('**/api/reading/**',r=>{const path=new URL(r.request().url()).pathname;if(path.endsWith('/messages'))submitted=r.request().postDataJSON();if(path.endsWith('/events'))return r.fulfill({contentType:'text/event-stream',body:'event: snapshot\ndata: '+JSON.stringify(session)+'\n\n'});return r.fulfill({json:path.endsWith('/settings')?{model:'fixture'}:session});});
await page.route('**/selection-fixture',r=>r.fulfill({contentType:'text/html',body:`<meta charset="utf-8"><div class="report-actions"><button id="report-library-add" data-arxiv-id="2401.12345v1" data-report-id="fixture">收藏</button></div><aside id="report-papers" hidden></aside><article class="report-article"><p id="passage">需要解释的论文内容</p><p id="second">另一个论文选区</p></article><p id="outside">正文之外</p><link rel="stylesheet" href="/static/reading-chat.css"><script src="/static/reading-chat.js"></script>`}));
await page.goto((process.env.PAPERLOOM_URL||'http://127.0.0.1:8000')+'/selection-fixture');await page.locator('.reading-model-label').filter({hasText:'fixture'}).waitFor({state:'attached'});
await page.locator('.reading-open').click();
await page.locator('textarea').fill('保留草稿');
await page.locator('.reading-open').click();
assert.equal(await page.locator('#report-reading').isHidden(),true,'second click must close the dock');
await page.locator('.reading-open').click();
assert.equal(await page.locator('textarea').inputValue(),'保留草稿');
const select=async id=>{await page.locator(id).evaluate(n=>{const r=document.createRange();r.selectNodeContents(n);const s=getSelection();s.removeAllRanges();s.addRange(r);});await page.waitForTimeout(100);};
await select('#passage');assert.equal(await page.locator('[data-action=explain]').isEnabled(),true,'selecting report text must enable Explain Selection');
await page.getByRole('button',{name:'解释选区',exact:true}).click();assert.equal(await page.locator('.reading-selection span').innerText(),'需要解释的论文内容');assert.match(await page.locator('textarea').inputValue(),/解释/);
await select('#second');assert.equal(await page.locator('.reading-selection span').innerText(),'另一个论文选区');
await select('#outside');assert.equal(await page.locator('.reading-selection span').innerText(),'另一个论文选区');
await page.getByRole('button',{name:'移除选区'}).click();assert.equal(await page.locator('[data-action=explain]').isDisabled(),true);
await select('#passage');await page.getByRole('button',{name:'解释选区',exact:true}).click();await Promise.all([page.waitForResponse(r=>r.url().endsWith('/messages') && r.request().method()==='POST'),page.getByRole('button',{name:'发送问题',exact:true}).click()]);assert.equal(submitted.selection,'需要解释的论文内容');assert.equal(submitted.report_id,'fixture');
assert.deepEqual(errors,[]);console.log('PASS live selection, replacement, outside exclusion, removal');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
