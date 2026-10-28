// NODE_PATH=work/browser-tools/node_modules node tests/browser/reading-workspace-resize.cjs
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:2048,height:1000}});
    // The previous outer-panel width must not keep the page pushed to the right.
    await page.addInitScript(()=>localStorage.setItem('paperloom.reading.workspace-width','1180'));
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
    const history = page.locator('#reading-panel>.reading-history');
    const chat = page.locator('#reading-panel>.reading-header');
    const handle = page.getByRole('separator', {name:'调整对话区域宽度'});
    const width = async () => history.evaluate(e=>e.offsetWidth);
    async function assertDividerPosition() {
      const boundary=await history.boundingBox(), hit=await handle.boundingBox();
      assert.ok(Math.abs(hit.x+hit.width/2-boundary.x-boundary.width)<2,
        'handle is on the internal history/chat divider');
    }
    await page.locator('.reading-reasoning').waitFor();
    assert.equal(await width(),320,'default history width');
    assert.match(await page.locator('.reading-reasoning').textContent(),/接口返回的思考摘要/);
    assert.equal(await history.isVisible(),true);
    await assertDividerPosition();
    const before = await panel.boundingBox(), hit = await handle.boundingBox();
    const chatBefore = await chat.boundingBox();
    assert.ok(before.x<32 && before.x+before.width>2016,'workspace fills the page, ignoring legacy outer width');
    await page.mouse.move(hit.x+4,hit.y+hit.height/2);
    await page.mouse.down();
    const scale=before.width/await panel.evaluate(e=>e.offsetWidth);
    await page.mouse.move(hit.x+4+200*scale,hit.y+hit.height/2,{steps:8});
    await page.mouse.up();
    assert.equal(await width(),520,'dragging divider right widens history');
    const chatAfter = await chat.boundingBox();
    assert.ok(Math.abs(chatBefore.width-chatAfter.width-200*scale)<2,'the same drag narrows the conversation');
    const after = await panel.boundingBox();
    assert.deepEqual(after,before,'outer workspace edges stay fixed');
    await assertDividerPosition();
    assert.equal(await page.locator('body').evaluate(e=>e.classList.contains('reading-resizing')),false);
    await page.reload();
    assert.equal(await width(),520,'saved split survives reload');
    await handle.focus();await page.keyboard.press('ArrowRight');
    assert.equal(await width(),540,'keyboard moves the internal divider');
    await page.keyboard.press('Home');assert.equal(await width(),220,'minimum history width');
    await page.keyboard.press('End');assert.equal(await width(),Number(await handle.getAttribute('aria-valuemax')),'maximum available width');
    const preferred=await width();
    await page.setViewportSize({width:900,height:700});
    await page.waitForFunction(()=>document.querySelector('.reading-history').offsetWidth<500);
    assert.ok(await chat.evaluate(e=>e.offsetWidth)>=360,'conversation keeps a usable minimum width');
    await assertDividerPosition();
    await page.setViewportSize({width:2048,height:1000});
    await page.waitForFunction(w=>document.querySelector('.reading-history').offsetWidth===w,preferred);
    await handle.dblclick();assert.equal(await width(),320,'double click resets the split');
    await page.locator('[data-action=close-history]').click();
    assert.equal(await handle.isVisible(),false,'no divider when history is closed');
    assert.deepEqual(await panel.boundingBox(),before,'closing history keeps workspace full width');
    await page.locator('[data-action=history]').click();
    assert.equal(await width(),320);
    await assertDividerPosition();
    await page.screenshot({path:'work/reading-workspace-resize-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await handle.isVisible(),false);
    assert.ok((await history.boundingBox()).width>360,'mobile history is an overlay, not a narrow column');
    assert.equal(await panel.evaluate(e=>e.scrollWidth<=e.clientWidth),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.locator('[data-action=close-history]').click();
    await page.screenshot({path:'work/reading-workspace-resize-mobile.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS internal divider position, inverse pane resizing, fixed full-width workspace, legacy preference, persistence, keyboard, reset, responsive history, reasoning display');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
