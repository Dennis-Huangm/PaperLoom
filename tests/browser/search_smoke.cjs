// Start `python tests/browser/serve_search_fixture.py`, then run this script.
// Optional isolated fixture URL: SEARCH_PREVIEW_URL=http://127.0.0.1:8769.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const base = process.env.SEARCH_PREVIEW_URL || 'http://127.0.0.1:8769';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/search-qa', {recursive: true});
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({width, height: 960});
    for (const [state, query] of [['initial', ''], ['results', '?q=奖励'], ['empty', '?q=no-match-zzzz']]) {
      await page.goto(base + '/search' + query);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${state} overflow at ${width}`);
      await page.screenshot({path: `work/search-qa/${state}-${width}.png`, fullPage: true});
    }
  }
  await page.setViewportSize({width: 1440, height: 960});
  await page.goto(base + '/search');
  await page.getByRole('searchbox', {name: '关键词', exact: true}).fill('奖励');
  await page.getByLabel('材料类型', {exact: true}).selectOption('notes');
  await page.getByLabel('修订版', {exact: true}).fill('2');
  await page.getByRole('button', {name: '搜索资料'}).click();
  assert.equal(new URL(page.url()).searchParams.get('kind'), 'notes');
  await page.getByRole('link', {name: '下一页'}).click();
  const searchURL = new URL(page.url());
  assert.equal(searchURL.searchParams.get('page'), '2');
  await page.locator('.search-result-card h3 a').first().click();
  assert((await page.locator('.search-source-text').textContent()).includes('<script>bad()</script>'));
  assert.equal(await page.locator('.search-source-text script').count(), 0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 960});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: `work/search-qa/source-${width}.png`, fullPage: true});
  }
  await page.getByRole('link', {name: '返回搜索'}).click();
  const restored = new URL(page.url());
  for (const key of ['q', 'kind', 'version', 'page', 'profile_id']) assert.equal(restored.searchParams.get(key), searchURL.searchParams.get(key), `Lost ${key}`);
  await page.getByRole('link', {name: '清除筛选'}).click();
  assert.equal(new URL(page.url()).searchParams.get('q'), '奖励');
  assert.equal(new URL(page.url()).searchParams.get('kind'), null);
  await page.locator('.search-index-panel details').first().locator('summary').click();
  assert.equal(await page.getByRole('button', {name: '重建全部索引'}).isVisible(), true);
  assert.deepEqual(errors, []);
  console.log('Search layout, form submission, pagination, source escaping, return context and reset passed.');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
