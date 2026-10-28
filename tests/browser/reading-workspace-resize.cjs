// NODE_PATH=work/browser-tools/node_modules node tests/browser/reading-workspace-resize.cjs
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:2048,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const session = {id:'fixture', mode:'workspace', title:'Fixture', references:[],
      messages:[{id:'answer', role:'assistant', status:'completed', text:'391', html:'<p>391</p>', reasoning:'接口返回的思考摘要。'}]};
    await page.route('http://fixture/**', route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path.startsWith('/static/')) return route.fulfill({
        contentType:path.endsWith('.js') ? 'application/javascript' : 'text/css',
        body:fs.readFileSync('src/paperloom'+path,'utf8')});
      if (path.includes('/api/reading/')) {
        if (path.endsWith('/settings')) return route.fulfill({json:{model:'fixture', reasoning_effort:'high'}});
        if (path.endsWith('/events')) return route.fulfill({contentType:'text/event-stream', body:': fixture\n\n'});
        if (path.endsWith('/sessions') && request.method()==='GET') return route.fulfill({json:[]});
        return route.fulfill({json:session});
      }
      return route.fulfill({contentType:'text/html', body:`<meta charset="utf-8">
        <link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/reading-chat.css">
        <body class="page-reading"><header class="app-header" style="height:60px">PaperLoom</header>
        <main class="app-main"><div id="reading-panel"></div></main>
        <script src="/static/reading-chat.js"></script></body>`});
    });
    await page.goto('http://fixture/');
    const panel = page.locator('#reading-panel');
    const handle = page.getByRole('separator', {name:'调整对话区域宽度'});
    const width = async () => panel.evaluate(e=>e.offsetWidth);
    await page.locator('.reading-reasoning').waitFor();
    assert.equal(await width(),1180,'narrower default on a wide screen');
    assert.match(await page.locator('.reading-reasoning').textContent(),/接口返回的思考摘要/);
    assert.equal(await page.locator('.reading-history').isVisible(),true);
    const before = await panel.boundingBox(), hit = await handle.boundingBox();
    await page.mouse.move(hit.x+4,hit.y+hit.height/2);
    await page.mouse.down();
    const scale=before.width/1180;
    await page.mouse.move(hit.x+4+200*scale,hit.y+hit.height/2,{steps:8});
    await page.mouse.up();
    assert.equal(await width(),980,'dragging the left edge right makes the panel narrower');
    const after = await panel.boundingBox();
    assert.equal(Math.round(before.x+before.width),Math.round(after.x+after.width),'right edge stays fixed');
    assert.equal(await page.locator('body').evaluate(e=>e.classList.contains('reading-resizing')),false);
    await page.reload();
    assert.equal(await width(),980,'saved width survives reload');
    await handle.focus();await page.keyboard.press('ArrowLeft');
    assert.equal(await width(),1000,'keyboard adjusts the same edge');
    await page.keyboard.press('Home');assert.equal(await width(),600,'minimum width');
    await page.keyboard.press('End');assert.equal(await width(),Number(await handle.getAttribute('aria-valuemax')),'maximum available width');
    await handle.dblclick();assert.equal(await width(),1180,'double click resets the default');
    await page.locator('[data-action=close-history]').click();
    assert.equal(await width(),1180,'closing history keeps panel width');
    await page.setViewportSize({width:900,height:700});
    await page.waitForFunction(()=>Math.round(document.querySelector('#reading-panel').getBoundingClientRect().width)===852);
    await page.setViewportSize({width:2048,height:1000});
    await page.waitForFunction(()=>document.querySelector('#reading-panel').offsetWidth===1180);
    await page.screenshot({path:'work/reading-workspace-resize-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await handle.isVisible(),false);
    assert.equal(await panel.evaluate(e=>e.scrollWidth<=e.clientWidth),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:'work/reading-workspace-resize-mobile.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS default width, drag, fixed right edge, persistence, keyboard, reset, history, responsive layout, reasoning display');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
