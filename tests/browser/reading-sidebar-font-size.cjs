// NODE_PATH=work/browser-tools/node_modules node tests/browser/reading-sidebar-font-size.cjs
const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');

(async()=>{
  const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[], settingsWrites=[];
    let failSettings=false;
    page.on('pageerror',error=>errors.push(error.message));
    const paper={title:'侧栏字号验证论文',arxiv_id:'2609.29718',version:2};
    const session={id:'font-fixture',title:'字号设置',paper,references:[],messages:[
      {id:'question',role:'user',text:'请解释论文的方法。'},
      {id:'answer',role:'assistant',status:'completed',text:'示例回答',html:'<h2>方法与实验</h2><p>这里是论文助手的回答，以及 <code>SVG</code> 示例。</p><blockquote>原文摘录</blockquote><table><tr><td>实验结果</td></tr></table><pre><code>answer = 42</code></pre>'},
    ]};
    await page.route('http://fixture/**',route=>{
      const request=route.request(),path=new URL(request.url()).pathname;
      if(path.startsWith('/static/'))return route.fulfill({
        contentType:path.endsWith('.js') ? 'application/javascript' : 'text/css',
        body:fs.readFileSync('src/paperloom'+path,'utf8'),
      });
      if(path.includes('/api/reading/')) {
        if(path.endsWith('/settings')) {
          if(request.method()==='PUT')settingsWrites.push(request.postDataJSON());
          return failSettings ? route.fulfill({status:503,json:{detail:'模型服务暂不可用'}}) : route.fulfill({json:{model:'fixture',max_tools:8,max_tokens:4096}});
        }
        if(path.endsWith('/events'))return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
        return route.fulfill({json:path.endsWith('/sessions') && request.method()==='GET' ? [] : session});
      }
      return route.fulfill({contentType:'text/html',body:`<meta charset="utf-8">
        <link rel="stylesheet" href="/static/reading-chat.css">
        ${path==='/workspace' ? '<div id="reading-panel"></div>' : '<div class="report-actions"><button id="report-library-add" data-arxiv-id="2609.29718v2">收藏</button></div><article class="report-article" style="font-size:18px">报告正文</article><aside id="report-papers" hidden></aside>'}
        <script src="/static/reading-chat.js"></script>`});
    });
    const fontSize=selector=>page.locator(selector).first().evaluate(node=>getComputedStyle(node).fontSize);
    const openSettings=async()=>{
      await page.locator('.reading-open').click();
      await page.locator('.reading-message-content p').waitFor();
      await page.locator('[data-action=settings]').click();
    };
    await page.goto('http://fixture/');
    await openSettings();
    const select=page.getByRole('combobox',{name:'侧栏字号'});
    assert.equal(await select.inputValue(),'13');
    assert.equal(await fontSize('.reading-message-content p'),'13px');
    await select.selectOption('20');
    for(const selector of ['.reading-message-content p','.reading-message.user .reading-message-content','.reading-compose textarea','.reading-message-content td','.reading-message-content blockquote','.reading-message-content p code']) {
      assert.equal(await fontSize(selector),'20px',selector);
    }
    assert.equal(await fontSize('.reading-message-content h2'),'21.6px');
    assert.equal(await fontSize('.report-article'),'18px','report size is independent');
    assert.equal(await fontSize('.reading-toolbar>[data-action=settings]'),'12px','toolbar keeps its size');
    assert.deepEqual(settingsWrites,[],'display preference saves without a model request');
    await page.locator('.reading-settings button[type=submit], .reading-settings .reading-primary').click();
    await page.waitForFunction(()=>!document.querySelector('dialog').open);
    assert.equal(settingsWrites.length,1);
    assert.equal(Object.keys(settingsWrites[0]).some(key=>/font/.test(key)),false);
    await page.reload();
    await openSettings();
    assert.equal(await select.inputValue(),'20','saved size survives reload');
    await page.setViewportSize({width:390,height:844});
    await select.selectOption('24');
    await page.screenshot({path:'work/reading-sidebar-font-settings-mobile.png'});
    await page.getByRole('button',{name:'关闭设置',exact:true}).click();
    assert.equal(await page.locator('#report-reading').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
    assert.equal(await page.locator('.reading-messages').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
    await page.screenshot({path:'work/reading-sidebar-font-content-mobile.png'});
    await page.locator('[data-action=settings]').click();
    await select.selectOption('13');
    assert.equal(await fontSize('.reading-message-content p'),'13px','default can be restored');
    await page.evaluate(()=>localStorage.setItem('paperloom.reading.sidebar-font-size','999'));
    failSettings=true;
    await page.reload();
    await openSettings();
    await page.waitForFunction(()=>document.querySelector('.reading-model-label').textContent.includes('暂不可用'));
    assert.equal(await select.inputValue(),'13','invalid preference falls back to default');
    assert.equal(await select.isEnabled(),true,'display settings work if model settings fail');
    await select.selectOption('18');
    await page.goto('http://fixture/workspace');
    assert.equal(await page.locator('.reading-font-size').count(),0,'preference belongs to report sidebar only');
    assert.equal(await fontSize('#reading-panel'),'14px');
    assert.deepEqual(errors,[]);
    console.log('PASS sidebar font setting, immediate sizing, model payload isolation, reload, reset, invalid preference, offline model settings, mobile layout, workspace isolation');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
