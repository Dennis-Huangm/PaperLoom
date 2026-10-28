// NODE_PATH=work/browser-tools/node_modules node tests/browser/reading-model.cjs
// Isolated UI contract; no local settings, credentials, or model calls.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    for (const mode of ['workspace', 'report']) {
      const page = await browser.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let globalModel = 'global-one';
      let settings = {independent:false, model:'saved-private', images:true,
        reasoning_effort:'', max_tools:10, max_tokens:4000, base_url:''};
      const session = {id:'fixture', paper:{arxiv_id:'2401.12345', version:1, title:'Fixture'},
        mode, title:'New conversation', references:[], messages:[]};
      await page.route('http://fixture/**', route => {
        const request = route.request(), path = new URL(request.url()).pathname;
        if (path.startsWith('/static/')) return route.fulfill({
          contentType:path.endsWith('.js') ? 'application/javascript' : 'text/css',
          body:fs.readFileSync('src/paperloom'+path,'utf8')});
        if (path.endsWith('/settings')) {
          if (request.method() === 'PUT') settings = {...settings, ...request.postDataJSON()};
          return route.fulfill({json:{...settings,
            effective_model:settings.independent ? settings.model : globalModel}});
        }
        if (path.includes('/api/reading/')) {
          if (path.endsWith('/events')) return route.fulfill({contentType:'text/event-stream', body:': fixture\n\n'});
          if (path.endsWith('/sessions') && request.method() === 'GET') return route.fulfill({json:[]});
          return route.fulfill({json:session});
        }
        const host = mode === 'workspace' ? '<div id="reading-panel"></div>' :
          '<article class="report-article">Fixture</article><aside id="report-papers" hidden></aside><div class="report-actions"><button id="report-library-add" data-arxiv-id="2401.12345v1">收藏</button></div>';
        return route.fulfill({contentType:'text/html', body:'<meta charset="utf-8">'+host+
          '<link rel="stylesheet" href="/static/reading-chat.css"><script src="/static/reading-chat.js"></script>'});
      });
      await page.goto('http://fixture/');
      if (mode === 'report') await page.locator('.reading-open').click();
      const label = page.locator('.reading-model-label');
      await page.waitForFunction(() => !document.querySelector('.reading-model-label').textContent.includes('加载'));
      assert.equal(await label.textContent(), 'global-one', mode+' inherits at startup');
      await label.click();
      const dialog = page.locator('dialog[open]');
      await dialog.locator('[name=independent]').check();
      await dialog.locator('[name=model]').fill('chosen-private');
      await dialog.getByRole('button',{name:'保存设置',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('.reading-model-label').textContent === 'chosen-private');
      await label.click();
      await page.locator('dialog[open] [name=independent]').uncheck();
      await page.locator('dialog[open]').getByRole('button',{name:'保存设置',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('.reading-model-label').textContent === 'global-one');

      // Returning after changing global settings updates the label, but never overwrites an open draft.
      await label.click();
      await page.locator('dialog[open] [name=model]').fill('unsaved-draft');
      globalModel = 'global-two';
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await page.waitForFunction(() => document.querySelector('.reading-model-label').textContent === 'global-two');
      assert.equal(await page.locator('dialog[open] [name=model]').inputValue(), 'unsaved-draft');
      await page.keyboard.press('Escape');
      await label.click();
      assert.equal(await page.locator('dialog[open] [name=model]').inputValue(), 'unsaved-draft');
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS inherited/independent label, save, global refresh and unsaved draft in both reading surfaces');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
