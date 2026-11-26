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
    await page.goto(base + '/profiles/versioned/edit');
    for (const width of [1920, 1024, 768, 390, 320]) {
      await page.setViewportSize({width, height: 800});
      const [editor, overview] = await Promise.all([
        page.locator('.profile-edit-form').boundingBox(),
        page.locator('[data-overview-panel]').boundingBox(),
      ]);
      if (width >= 1024) {
        assert.ok(editor.x + editor.width < overview.x, `side-by-side at ${width}`);
        assert.equal(editor.y, overview.y);
      } else {
        assert.ok(overview.y + overview.height < editor.y, `stacked at ${width}`);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `editor overflow at ${width}`);
      const save = await page.getByRole('button', {name: '保存方向'}).boundingBox();
      assert.ok(save && save.y >= 0 && save.y + save.height <= 800, `save remains visible at ${width}`);
      await page.screenshot({path: `work/overview-qa/edit-${width}.png`});
    }
    await page.setViewportSize({width: 1440, height: 1000});
    const topics = page.locator('.profile-edit-search .draft-group').first();
    await topics.locator(':scope > summary').click();
    const condition = topics.locator('.draft-condition').first();
    await condition.locator('summary').click();
    await condition.getByRole('textbox', {name: '条件', exact: true}).fill('controllable SVG generation');
    await condition.locator('summary').click();
    await page.getByRole('button', {name: '保存方向'}).click();
    await page.waitForURL(url => url.searchParams.get('updated') === 'versioned');
    await page.goto(base + '/profiles/versioned/edit');
    assert.match(await topics.textContent(), /controllable SVG generation/);

    await page.goto(base + '/profiles/svg/overview');
    const downloadEvent = page.waitForEvent('download');
    await page.getByRole('link', {name: '下载 Markdown'}).click();
    assert.match((await downloadEvent).suggestedFilename(), /svg-overview\.md/);
    await page.getByRole('button', {name: '重新生成小综述'}).click();
    await page.waitForFunction(() => document.querySelector('[data-overview-panel]').dataset.overviewStatus === 'ready');
    // A background completion must not discard unsaved direction edits.
    await page.request.post(base + '/api/direction-overviews/versioned');
    await page.goto(base + '/profiles/versioned/edit');
    await page.locator('[data-overview-progress]').waitFor();
    await page.screenshot({path: 'work/overview-qa/edit-generating.png'});
    await page.locator('input[name="name"]').fill('尚未保存的名称');
    await page.waitForFunction(() => document.querySelector('[data-overview-panel]').dataset.overviewStatus === 'ready');
    assert.match(await page.locator('[data-overview-progress]').textContent(), /当前修改仍可保存/);
    assert.equal(await page.getByRole('link', {name: '阅读方向小综述'}).getAttribute('target'), '_blank');
    assert.equal(await page.locator('input[name="name"]').inputValue(), '尚未保存的名称');
    assert.deepEqual(errors, []);
    console.log('Overview reading, responsive editor, condition editing, download, polling, and unsaved edits passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
