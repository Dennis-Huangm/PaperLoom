// Start python tests/browser/serve_report_fixture.py, then run this script.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage({viewport: {width: 1920, height: 1080}});
  const base = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
  const firstReport = base + '/artifacts/2026-10-03/reports/paper-0/report.html';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/report-qa', {recursive: true});
  await page.goto(firstReport);
  await page.waitForSelector('.report-paper-link');
  assert.equal(await page.locator('.report-paper-link').count(), 3);
  assert.equal(await page.locator('.report-paper-link[aria-current="page"]').count(), 1);
  assert.equal(Math.round((await page.locator('#report-papers').boundingBox()).width), 320);
  const sharedAppearance = await page.evaluate(() => {
    function appearance(node) {
      const style = getComputedStyle(node);
      return [style.backgroundColor, style.borderColor, style.borderRadius, style.padding, style.fontSize];
    }
    return {
      left: appearance(document.querySelector('#report-toc-close')),
      right: appearance(document.querySelector('#report-papers-close')),
      leftPanel: appearance(document.querySelector('.report-sidebar')),
      rightPanel: appearance(document.querySelector('#report-papers')),
    };
  });
  assert.deepEqual(sharedAppearance.left, sharedAppearance.right, 'collapse buttons have matching appearance');
  assert.deepEqual(sharedAppearance.leftPanel, sharedAppearance.rightPanel, 'panels have matching appearance');
  const originalArticleWidth = (await page.locator('.report-article').boundingBox()).width;
  await page.locator('#report-toc-close').click();
  assert.equal(await page.locator('.report-sidebar').isVisible(), false);
  assert((await page.locator('.report-article').boundingBox()).width > originalArticleWidth);
  await page.reload();
  await page.waitForSelector('.report-paper-link');
  assert.equal(await page.locator('.report-sidebar').isVisible(), false, 'TOC preference survives refresh');
  await page.locator('#report-papers-close').click();
  const fullArticleWidth = (await page.locator('.report-article').boundingBox()).width;
  await page.locator('#report-papers-toggle').click();
  assert((await page.locator('.report-article').boundingBox()).width < fullArticleWidth);
  assert.equal(await page.locator('.report-sidebar').isVisible(), false, 'right sidebar leaves TOC preference intact');
  await page.locator('#report-toc-toggle').click();
  assert.equal(await page.locator('.report-sidebar').isVisible(), true);
  const handle = await page.locator('#report-papers-resize').boundingBox();
  await page.mouse.move(handle.x + 6, handle.y + 100);
  await page.mouse.down();
  await page.mouse.move(handle.x - 54, handle.y + 100);
  await page.mouse.up();
  assert.equal(Math.round((await page.locator('#report-papers').boundingBox()).width), 380);
  await page.screenshot({path: 'work/report-qa/desktop.png'});
  await page.locator('#report-papers-search').fill('Ada');
  assert.equal(await page.locator('.report-paper-link').count(), 3);
  await page.locator('#report-papers-search').fill('2608.01977v1');
  assert.equal(await page.locator('.report-paper-link').count(), 1);
  await page.locator('#report-papers-search').fill('Visual Agents');
  assert.equal(await page.locator('.report-paper-link').count(), 1);
  await page.locator('#report-toc-close').click();
  await page.locator('.report-paper-link').click();
  await page.waitForURL('**/paper-1/report.html');
  await page.waitForSelector('.report-paper-link');
  assert.equal(await page.locator('#report-papers-search').inputValue(), 'Visual Agents');
  assert.equal(await page.locator('.report-sidebar').isVisible(), false, 'TOC preference survives report navigation');
  assert.equal(Math.round((await page.locator('#report-papers').boundingBox()).width), 380);
  assert.equal(await page.evaluate(() => scrollY), 0);
  await page.goBack();
  await page.waitForURL('**/paper-0/report.html');
  await page.waitForSelector('.report-paper-link');
  await page.locator('#report-toc-toggle').click();
  await page.locator('#report-papers-search').fill('no-such-paper');
  assert.equal(await page.locator('#report-papers-clear').isVisible(), true);
  await page.locator('#report-papers-clear').click();
  assert.equal(await page.locator('.report-paper-link').count(), 3);
  await page.locator('#report-papers-close').click();
  await page.reload();
  assert.equal(await page.locator('#report-papers').isVisible(), false);
  await page.locator('#report-papers-toggle').click();
  await page.waitForSelector('.report-paper-link');
  for (const width of [1440, 1100, 768, 390, 320]) {
    await page.setViewportSize({width, height: 900});
    if (width < 1440) {
      await page.waitForFunction(() => !document.querySelector('.report-shell').classList.contains('papers-open'));
      if (!await page.locator('#report-papers').isVisible()) await page.locator('#report-papers-toggle').click();
      assert.equal(await page.locator('#report-papers').getAttribute('role'), 'dialog');
      assert.equal(await page.locator('#report-papers-resize').isVisible(), false);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
    await page.screenshot({path: `work/report-qa/width-${width}.png`});
  }
  await page.locator('#report-papers-search').fill('Visual Agents');
  await page.locator('.report-paper-link').click();
  await page.waitForURL('**/paper-1/report.html');
  await page.waitForSelector('.report-paper-link');
  assert.equal(await page.locator('#report-papers').isVisible(), true, 'drawer persists on navigation');
  await page.locator('#report-papers-close').click();
  await page.locator('.toc-toggle').click();
  assert.equal(await page.locator('#report-papers').isVisible(), false);
  assert.equal(await page.locator('.report-sidebar').evaluate(node => node.classList.contains('open')), true);
  assert.equal(await page.locator('.report-article').evaluate(node => node.inert), true);
  const tocScroll = await page.locator('.report-toc').evaluate(node => {
    node.scrollTop = node.scrollHeight;
    return {scrollTop: node.scrollTop, clientHeight: node.clientHeight, scrollHeight: node.scrollHeight};
  });
  assert(tocScroll.scrollTop > 0, 'long mobile TOC has an independent scroll area');
  assert.equal(tocScroll.scrollTop + tocScroll.clientHeight, tocScroll.scrollHeight, 'TOC scroll reaches the end');
  const lastTocLink = await page.locator('.report-toc a').last().boundingBox();
  assert(lastTocLink.y + lastTocLink.height <= 900, 'last TOC entry is inside the viewport');
  await page.locator('#report-toc-backdrop').click({position: {x: 315, y: 300}});
  assert.equal(await page.locator('.report-sidebar').isVisible(), false);
  await page.locator('.toc-toggle').click();
  await page.locator('#report-toc-close').click();
  assert.equal(await page.locator('.report-sidebar').isVisible(), false);
  await page.locator('.toc-toggle').click();
  await page.locator('#report-papers-toggle').click();
  assert.equal(await page.locator('.report-sidebar').evaluate(node => node.classList.contains('open')), false);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#report-papers').isVisible(), false);
  await page.locator('#report-papers-toggle').click();
  await page.locator('#report-papers-backdrop').click({position: {x: 3, y: 300}});
  assert.equal(await page.locator('#report-papers').isVisible(), false);
  await page.route('**/api/reports', route => route.fulfill({status: 503, body: '{}'}));
  await page.locator('#report-papers-toggle').click();
  await page.waitForSelector('#report-papers-retry:not([hidden])');
  await page.unroute('**/api/reports');
  await page.locator('#report-papers-retry').click();
  await page.waitForSelector('.report-paper-link');
  await page.emulateMedia({media: 'print'});
  assert.equal(await page.locator('#report-papers').isVisible(), false);
  assert.equal(await page.locator('#report-papers-backdrop').isVisible(), false);
  await page.emulateMedia({media: 'screen'});
  // A saved older document still references the current shared script.
  await page.route('**/report.html', async route => {
    const response = await route.fetch();
    const body = (await response.text())
      .replace(/<button id="report-toc-toggle"[\s\S]*?<\/button>/, '')
      .replace(/<button id="report-toc-close"[\s\S]*?<\/button>/, '')
      .replace('class="report-sidebar report-nav-panel"', 'class="report-sidebar"');
    await route.fulfill({response, body});
  });
  await page.evaluate(() => sessionStorage.removeItem('paperloom.reportPapers.drawerOpen'));
  await page.goto(firstReport);
  await page.locator('.toc-toggle').click();
  assert.equal(await page.locator('.report-sidebar').isVisible(), true, 'legacy TOC still opens');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.report-sidebar').isVisible(), false);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Report catalog, resize, search, navigation, persistence, mobile drawers, retry and print passed.');
})().catch(error => { console.error(error); process.exit(1); });
