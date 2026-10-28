// Run with NODE_PATH pointing to a Playwright installation; no live data/model calls.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const session = {id:'fixture', paper:{}, mode:'workspace', title:'新阅读对话', references:[], messages:[]};
(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [], pending = {}, requests = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://fixture/**', route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if(path.startsWith('/static/')) return route.fulfill({contentType:path.endsWith('.js')?'application/javascript':'text/css', body:fs.readFileSync('src/paperloom'+path,'utf8')});
      if(path.startsWith('/api/reading/')) {
        requests.push({path, method:request.method(), profile:request.headers()['x-paperloom-profile']});
        if(path.endsWith('/settings')) {pending.settings=route;return;}
        if(path.endsWith('/events')) return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
        if(path.endsWith('/sessions') && request.method()==='GET') return route.fulfill({json:[]});
        if(path.endsWith('/sessions')) {pending.create=route;pending.notifyCreate?.();return;}
        pending.detail=route;pending.notifyDetail?.();return;
      }
      return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="reading-panel" data-profile-id="original"></div><link rel="stylesheet" href="/static/reading-chat.css"><script src="/static/reading-chat.js"></script>'});
    });
    await page.goto('http://fixture/');
    assert.equal(await page.locator('.reading-welcome').count(),1,'welcome must exist while settings/session are pending');
    assert.equal(await page.locator('.reading-history').isVisible(),true,'desktop history layout must not await session');
    assert.equal(await page.getByRole('button',{name:'发送问题',exact:true}).isDisabled(),true);
    await page.getByRole('textbox',{name:'向论文提问'}).fill('等待时写下的问题');
    const before=await page.locator('.reading-messages').boundingBox();
    await pending.settings.fulfill({status:503,json:{detail:'设置暂不可用'}});
    assert.ok(pending.create,'session startup must be independent of settings failure');
    const detailRequest=new Promise(resolve=>pending.notifyDetail=resolve);
    await pending.create.fulfill({json:session});
    await detailRequest;
    await pending.detail.fulfill({json:session});
    await page.waitForFunction(()=>!document.querySelector('[data-action=add-references]').disabled);
    assert.equal(await page.getByRole('textbox',{name:'向论文提问'}).inputValue(),'等待时写下的问题');
    assert.equal((await page.locator('.reading-messages').boundingBox()).x,before.x);
    assert.ok(requests.filter(r=>!r.path.endsWith('/events')).every(r=>r.profile==='original'),'requests keep the page profile even without settings');
    // A failed startup can be retried without losing the question or changing context.
    let created=new Promise(resolve=>pending.notifyCreate=resolve);
    await page.locator('[data-action=new]').click();await created;
    await page.getByRole('textbox',{name:'向论文提问'}).fill('失败后保留的问题');
    await pending.create.fulfill({status:503,json:{detail:'会话暂不可用'}});
    await page.getByRole('button',{name:'重试加载会话',exact:true}).waitFor();
    created=new Promise(resolve=>pending.notifyCreate=resolve);
    await page.getByRole('button',{name:'重试加载会话',exact:true}).click();await created;
    const retried=new Promise(resolve=>pending.notifyDetail=resolve);
    await pending.create.fulfill({json:session});await retried;
    await pending.detail.fulfill({json:session});
    await page.waitForFunction(()=>!document.querySelector('[data-action=add-references]').disabled);
    assert.equal(await page.getByRole('textbox',{name:'向论文提问'}).inputValue(),'失败后保留的问题');
    // A superseded startup must not replace a newer conversation or its readiness.
    created=new Promise(resolve=>pending.notifyCreate=resolve);
    await page.locator('[data-action=new]').click();await created;const stale=pending.create;
    created=new Promise(resolve=>pending.notifyCreate=resolve);
    await page.locator('[data-action=new]').click();await created;
    const latest=new Promise(resolve=>pending.notifyDetail=resolve);
    await pending.create.fulfill({json:session});await latest;await pending.detail.fulfill({json:session});
    await page.waitForFunction(()=>!document.querySelector('[data-action=add-references]').disabled);
    await stale.fulfill({status:503,json:{detail:'过期失败不得覆盖新会话'}});
    assert.equal(await page.getByRole('button',{name:'重试加载会话',exact:true}).isVisible(),false);
    assert.equal(await page.getByRole('textbox',{name:'向论文提问'}).isEnabled(),true);
    assert.deepEqual(errors,[]);
    console.log('PASS welcome/history before data, settings failure isolation, stable layout, draft and profile');
    await page.close();
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
