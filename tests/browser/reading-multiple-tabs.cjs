// Run serve_report_fixture.py first. Real HTTP/1.1 connections are essential:
// mocked EventSource responses cannot reproduce browser connection starvation.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const base = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
  try {
    const context = await browser.newContext();
    const pages = [];
    const errors = [], details = [];
    context.on('page', page=>page.on('pageerror',error=>errors.push(error.message)));
    context.on('request',request=>{
      if(/\/api\/reading\/sessions\/[^/]+$/.test(new URL(request.url()).pathname) && request.method()==='GET')details.push(request);
    });
    // Sequential startup isolates the limit; keep every earlier tab open.
    for(let index=0; index<10; index++) {
      const page = await context.newPage();pages.push(page);
      await page.goto(base+'/artifacts/2026-10-03/reports/paper-'+index%3+'/report.html', {timeout:8000});
      await page.waitForFunction(()=>!document.querySelector('#report-reading [type=submit]').disabled);
      await page.waitForTimeout(200);
      console.log('Loaded report tab '+(index+1));
    }
    const home = await context.newPage();
    assert.equal((await home.goto(base+'/', {timeout:8000})).status(),200);
    // Opening every dock must also leave connections available.
    await Promise.all(pages.map(page=>page.locator('.reading-open').click()));
    assert.equal((await home.reload({timeout:8000})).status(),200);
    const page=pages[0];
    const detail=details.find(request=>request.frame().page()===page).url();
    await page.getByRole('button',{name:'收起阅读助手',exact:true}).click();
    const before=details.filter(request=>request.frame().page()===page).length;
    await page.waitForTimeout(5500);
    assert.equal(details.filter(request=>request.frame().page()===page).length,before,'closed dock must stop polling');
    const resumed=page.waitForResponse(response=>response.url()===detail);
    await page.locator('.reading-open').click();await resumed;
    assert.equal((await context.request.delete(detail)).status(),200);
    await page.waitForFunction(()=>document.querySelector('.reading-status').textContent==='会话已删除',{},{timeout:8000});
    // Also cover a burst of navigations while the existing tabs stay open.
    await Promise.all(Array.from({length:10},async (_,index)=>{
      const next=await context.newPage();
      const response=await next.goto(base+'/artifacts/2026-10-03/reports/paper-'+index%3+'/report.html',{timeout:8000});
      assert.equal(response.status(),200);
      await next.waitForFunction(()=>!document.querySelector('#report-reading [type=submit]').disabled);
    }));
    assert.equal((await home.reload({timeout:8000})).status(),200);
    assert.deepEqual(errors,[]);
    console.log('PASS twenty tabs including ten simultaneous navigations, open docks, homepage, pause/resume and cross-tab deletion');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
