// Start python tests/browser/serve_overview_fixture.py before running.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const base = process.env.OVERVIEW_PREVIEW_URL || 'http://127.0.0.1:8776';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/overview-qa', {recursive: true});
  try {
    for (const width of [1440, 1024, 768, 390]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(base + '/profiles/svg/overview');
      await page.locator('.overview-article h2').first().waitFor();
      assert.equal(await page.locator('.overview-article h2').count(), 6);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
      assert.match(await page.locator('.overview-article').textContent(), /入门阅读顺序/);
      assert.ok(await page.locator('.overview-article a[href="https://arxiv.org/abs/2401.00001v2"]').count());
      await page.screenshot({path: `work/overview-qa/reading-${width}.png`, fullPage: true});
    }
    await page.setViewportSize({width: 1440, height: 1000});
    const downloadEvent = page.waitForEvent('download');
    await page.getByRole('link', {name: '下载 Markdown'}).click();
    assert.match((await downloadEvent).suggestedFilename(), /svg-overview\.md/);
    await page.getByRole('button', {name: '重新生成小综述'}).click();
    await page.waitForFunction(() => document.querySelector('[data-overview-panel]').dataset.overviewStatus === 'ready');
    // A background completion must not discard unsaved direction edits.
    await page.request.post(base + '/api/direction-overviews/svg');
    await page.goto(base + '/profiles/svg/edit');
    await page.locator('input[name="name"]').fill('尚未保存的名称');
    await page.waitForFunction(() => document.querySelector('[data-overview-status-label]').textContent.includes('保存修改后刷新'));
    assert.equal(await page.locator('input[name="name"]').inputValue(), '尚未保存的名称');
    assert.deepEqual(errors, []);
    console.log('Overview reading, responsive layout, download, polling, and unsaved edits passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
