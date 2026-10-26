// Start python tests/browser/serve_conferences_fixture.py before running.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const base = process.env.CONFERENCES_PREVIEW_URL || 'http://127.0.0.1:8775';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/conferences-qa', {recursive: true});
  const failedURL = base + '/conferences?search_id=' + 'a'.repeat(32);
  const successURL = base + '/conferences?search_id=' + 'b'.repeat(32);
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({width, height: 1000});
    for (const [state, url] of [['incomplete', failedURL], ['success', successURL]]) {
      await page.goto(url);
      await page.locator('[data-job-id]').first().waitFor({state: 'attached'});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${state}: overflow at ${width}`);
      assert.equal(await page.locator('.conference-history-item').count(), 2);
      assert.equal(await page.locator('.conference-error-details[open]').count(), 0);
      await page.locator('#conference-results').scrollIntoViewIfNeeded();
      await page.screenshot({path: `work/conferences-qa/${state}-${width}.png`, fullPage: true});
      if (width === 1440) await page.screenshot({path: `work/conferences-qa/${state}-detail.png`});
    }
  }
  await page.goto(failedURL);
  const detail = page.locator('.conference-source-card .conference-error-details');
  await detail.locator('summary').click();
  assert.match(await detail.textContent(), /404 Not Found/);
  assert.equal(await detail.getAttribute('open'), '');
  await page.locator('.conference-topic summary').click();
  assert.match(await page.locator('.conference-topic p').textContent(), /研究主题/);
  await page.getByRole('link', {name: /NeurIPS · 2025–2026/}).filter({has: page.locator('b.is-complete')}).click();
  await page.waitForURL('**search_id=' + 'b'.repeat(32) + '#conference-results');
  assert.equal(await page.locator('.search-result-card').count(), 1);
  const record = page.locator('[data-job-id]').filter({hasText: '之前失败的检索'});
  await page.locator('.conference-task-history>summary').click();
  const jobId = await record.getAttribute('data-job-id');
  // Declining confirmation preserves the task; accepting removes it durably.
  page.once('dialog', dialog => dialog.dismiss());
  await record.getByRole('button', {name: /删除任务记录/}).click();
  assert.equal(await record.count(), 1);
  page.once('dialog', dialog => dialog.accept());
  await record.getByRole('button', {name: /删除任务记录/}).click();
  await record.waitFor({state: 'detached'});
  await page.reload();
  assert.equal(await page.locator(`[data-job-id="${jobId}"]`).count(), 0);
  assert.equal(await page.locator('.conference-history-item').count(), 2);
  // Delete an older failed result while keeping the selected successful result.
  const failedHistory = page.locator('.conference-history-item').filter({hasText: '来源未完成'});
  page.once('dialog', dialog => dialog.accept());
  await failedHistory.getByRole('button', {name: '删除记录', exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll('.conference-history-item').length === 1);
  assert(new URL(page.url()).searchParams.get('search_id') === 'b'.repeat(32));
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name: '删除本次记录', exact: true}).click();
  await page.getByText('暂无历史记录', {exact: true}).waitFor();
  await page.reload();
  assert.equal(await page.locator('.conference-history-item').count(), 0);
  assert.equal(await page.getByRole('heading', {name: '开始一次会议论文检索'}).isVisible(), true);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Conference responsive layouts, error disclosure, history navigation and durable deletion passed.');
})().catch(error => {console.error(error); process.exit(1);});
