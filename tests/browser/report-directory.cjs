// Start python tests/browser/serve_report_catalog_fixture.py; uses disposable data.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage({reducedMotion: 'reduce'});
  const base = process.env.REPORT_CATALOG_PREVIEW_URL || 'http://127.0.0.1:8794';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/report-directory-qa', {recursive: true});
  const panel = page.locator('#report-directory');
  const drawer = page.locator('#report-directory-dialog');
  const toggle = page.locator('#report-directory-toggle');
  const close = page.locator('#report-directory-close');
  const links = page.locator('.report-directory-link');
  const rows = page.locator('.report-row');

  const matchingEntries = async count => {
    assert.equal(await links.count(), count);
    assert.equal(await rows.count(), count);
    assert.match(await panel.locator('header p').textContent(), new RegExp(`· ${count} 篇`));
    assert.deepEqual(await links.locator('strong').allTextContents(), await rows.locator('h2').allTextContents());
    for (const href of await links.evaluateAll(nodes => nodes.map(node => node.getAttribute('href')))) {
      assert.equal(await page.locator(href).count(), 1);
    }
  };
  const alignedWithHeader = async index => {
    await page.waitForFunction(index => {
      const row = document.querySelectorAll('.report-row')[index];
      const header = document.querySelector('.app-header');
      return row.getBoundingClientRect().top >= header.getBoundingClientRect().bottom;
    }, index);
    assert.equal(await rows.nth(index).evaluate(node => node === document.activeElement), true);
    assert.equal(await links.nth(index).getAttribute('aria-current'), 'location');
  };

  for (const width of [3840, 1920, 1440, 1200, 1100, 1024, 768, 390, 320]) {
    await page.setViewportSize({width, height: 1000});
    await page.goto(base + '/reports');
    await matchingEntries(32);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
    if (width > 1100) {
      assert.equal(await panel.isVisible(), true);
      assert.equal(await toggle.isVisible(), false);
      await page.waitForFunction(() => document.querySelector('#report-directory').getBoundingClientRect().bottom <= innerHeight);
      if (width === 1440) await page.screenshot({path: 'work/report-directory-qa/reports-desktop.png'});
      const before = await page.evaluate(() => scrollY);
      await page.locator('.report-directory-list').evaluate(node => { node.scrollTop = node.scrollHeight; });
      assert.equal(await page.evaluate(() => scrollY), before);
      await links.nth(25).click();
      await alignedWithHeader(25);
      assert.equal(new URL(page.url()).pathname, '/reports');
      const panelBox = await panel.boundingBox();
      assert(panelBox.y >= 0 && panelBox.y + panelBox.height <= 1001, `directory height at ${width}`);
      await close.click();
      assert.equal(await panel.isVisible(), false);
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      await toggle.click();
      assert.equal(await panel.isVisible(), true);
    } else {
      assert.equal(await panel.isVisible(), false);
      await toggle.click();
      assert.equal(await drawer.evaluate(node => node.matches(':modal')), true);
      assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('#report-directory-dialog').open);
      assert.equal(await toggle.evaluate(node => node === document.activeElement), true);
      await toggle.click();
      await links.nth(25).click();
      await alignedWithHeader(25);
      assert.equal(await drawer.evaluate(node => node.open), false);
      assert.equal(await page.evaluate(() => document.body.classList.contains('report-directory-modal-open')), false);
      await toggle.click();
      await page.screenshot({path: `work/report-directory-qa/drawer-${width}.png`});
      await close.click();
    }
    await page.screenshot({path: `work/report-directory-qa/reports-${width}.png`});
  }

  await page.setViewportSize({width: 1440, height: 900});
  await page.goto(base + '/reports');
  // Real page scrolling updates the directory without a directory click.
  await rows.nth(18).evaluate(node => node.scrollIntoView({block: 'start', behavior: 'instant'}));
  await page.waitForFunction(() => document.querySelectorAll('.report-directory-link')[18].getAttribute('aria-current') === 'location');
  const activeBox = await links.nth(18).boundingBox();
  const listBox = await page.locator('.report-directory-list').boundingBox();
  assert(activeBox.y >= listBox.y && activeBox.y + activeBox.height <= listBox.y + listBox.height + 1);
  await page.evaluate(() => window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'}));
  await page.waitForFunction(() => document.querySelector('.report-directory-link:last-child').hasAttribute('aria-current'));
  await page.screenshot({path: 'work/report-directory-qa/reports-desktop-scrolled.png'});

  // Switching viewport while the modal is open releases background and focus.
  await page.setViewportSize({width: 390, height: 844});
  await toggle.click();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.querySelector('#report-directory-dialog').contains(document.activeElement)), true);
  await page.setViewportSize({width: 1440, height: 900});
  assert.equal(await drawer.evaluate(node => node.open), false);
  assert.equal(await panel.isVisible(), true);
  assert.equal(await page.evaluate(() => document.body.classList.contains('report-directory-modal-open')), false);

  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.goto(base + '/reports');
  await links.nth(20).click();
  await page.waitForFunction(() => {
    const row = document.querySelectorAll('.report-row')[20];
    const gap = row.getBoundingClientRect().top - document.querySelector('.app-header').getBoundingClientRect().bottom;
    return gap >= 0 && gap < 40 && row.classList.contains('is-current');
  });
  await page.emulateMedia({reducedMotion: 'reduce'});

  // Bulk selection remains separate from navigation and survives directory clicks.
  await page.goto(base + '/reports');
  await rows.first().locator('.report-selection').check();
  await links.nth(2).click();
  assert.equal(await page.locator('#report-selection-count').textContent(), '已选择 1 篇');
  assert.equal(await rows.first().locator('.report-selection').isChecked(), true);
  await page.locator('#report-select-all').check();
  assert.equal(await page.locator('#report-selection-count').textContent(), '已选择 32 篇');
  await page.locator('#report-selection-clear').click();

  for (const [order, firstId] of [
    ['added_asc', '2608.02008'], ['added_desc', '2608.01977'],
    ['published_desc', '2608.02004'], ['published_asc', '2608.02005'],
  ]) {
    await page.getByLabel('排序', {exact: true}).selectOption(order);
    await page.waitForURL(url => url.searchParams.get('sort') === order);
    await matchingEntries(32);
    assert.match(await rows.first().locator('.report-id').textContent(), new RegExp(firstId.replace('.', '\\.')));
    if (order.startsWith('published')) {
      const dates = await rows.locator('.report-dates span:last-child').allTextContents();
      assert(dates.slice(0, -2).every(date => !date.includes('未知')));
      assert(dates.slice(-2).every(date => date.includes('未知')));
    }
    await links.nth(5).click();
    await alignedWithHeader(5);
  }
  await page.getByLabel('按研究方向筛选').selectOption('alpha');
  await page.getByRole('button', {name: '搜索', exact: true}).click();
  await matchingEntries(16);
  assert.equal(new URL(page.url()).searchParams.get('sort'), 'published_asc');
  await page.getByRole('searchbox', {name: '标题、作者或 arXiv ID'}).fill('MoVer');
  await page.getByRole('button', {name: '搜索', exact: true}).click();
  await matchingEntries(1);
  await page.getByRole('link', {name: '清除搜索', exact: true}).click();
  await matchingEntries(16);
  assert.equal(new URL(page.url()).searchParams.get('sort'), 'published_asc');
  assert.equal(new URL(page.url()).searchParams.get('direction'), 'alpha');
  assert.equal(await rows.first().locator('h2 a').getAttribute('href') !== '#report-entry-1', true);
  await page.getByRole('searchbox', {name: '标题、作者或 arXiv ID'}).fill('no-match-zzzz');
  await page.getByRole('button', {name: '搜索', exact: true}).click();
  await matchingEntries(0);
  assert.equal(await page.getByRole('heading', {name: '没有匹配的报告'}).isVisible(), true);
  assert.equal(await panel.locator('.report-directory-empty').isVisible(), true);
  await page.screenshot({path: 'work/report-directory-qa/reports-empty.png'});

  // Titles containing markup remain text; anchors also work with JS disabled.
  const plain = await browser.newPage({javaScriptEnabled: false, viewport: {width: 1440, height: 900}});
  await plain.goto(base + '/reports');
  assert.equal(await plain.locator('.report-directory-list script').count(), 0);
  await plain.locator('.report-directory-link').nth(20).click();
  assert.match(plain.url(), /#report-entry-21$/);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Report directory: responsive layout, scrolling, focus, selection, four sorting modes, filtering, empty results and no-JS anchors passed.');
})().catch(error => {console.error(error); process.exit(1);});
