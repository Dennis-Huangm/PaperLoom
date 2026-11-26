// Start serve_report_catalog_fixture.py with disposable data before running.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 900}, reducedMotion: 'reduce'});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.env.REPORT_CATALOG_PREVIEW_URL || 'http://127.0.0.1:8794';
    const order = () => page.locator('.report-row').evaluateAll(rows => rows.map(row => row.dataset.reportId));
    const rowFor = id => page.locator('.report-row').filter({has: page.locator(`.report-selection[value="${id}"]`)});
    const syncDirectory = async () => {
      assert.deepEqual(await page.locator('.report-directory-copy strong').allTextContents(),
        await page.locator('.report-row h2').allTextContents());
      assert.deepEqual(await page.locator('.report-directory-number').allTextContents(),
        Array.from({length: 32}, (_, index) => String(index + 1)));
    };
    const returnToList = async (event = 'focus') => {
      await page.bringToFront();
      await page.evaluate(event => {
        if (event === 'pageshow') window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        else if (event === 'visibilitychange') document.dispatchEvent(new Event(event));
        else window.dispatchEvent(new Event(event));
      }, event);
    };
    const waitFirst = async id => page.waitForFunction(id => document.querySelector('.report-row').dataset.reportId === id, id);

    await page.goto(base + '/reports');
    const original = await order();
    assert.equal(original.length, 32);
    assert.equal(await page.getByLabel('排序', {exact: true}).inputValue(), 'opened_desc');
    const first = original[6];
    const second = original[12];
    const firstUrl = await rowFor(first).locator('h2 a').getAttribute('href');
    const secondUrl = await rowFor(second).locator('h2 a').getAttribute('href');

    // Directory navigation and bulk selection alone do not count as an open.
    await rowFor(original[20]).locator('.report-selection').check();
    await page.locator('.report-directory-link').nth(8).click();
    // Hold an old snapshot while opening/closing a report. The return event must
    // queue a fresh query rather than being dropped behind the pending request.
    let captured;
    let release;
    let hold = true;
    const staleCaptured = new Promise(resolve => { captured = resolve; });
    const releaseStale = new Promise(resolve => { release = resolve; });
    await page.route('**/reports', async route => {
      const response = await route.fetch();
      if (hold) {
        hold = false;
        captured();
        await releaseStale;
      }
      await route.fulfill({response});
    });
    await returnToList();
    await staleCaptured;
    assert.deepEqual(await order(), original);

    const popupPromise = page.waitForEvent('popup');
    await rowFor(first).locator('h2 a').click();
    const popup = await popupPromise;
    await popup.waitForLoadState();
    await popup.close();
    await returnToList();
    release();
    await waitFirst(first);
    await page.unroute('**/reports');
    assert.deepEqual(await order(), [first, ...original.filter(id => id !== first)]);
    assert.equal(await rowFor(original[20]).locator('.report-selection').isChecked(), true);
    assert.equal(await page.locator('#report-selection-count').textContent(), '已选择 1 篇');
    await syncDirectory();

    // A moved link still navigates to its own row rather than its original index.
    const movedLink = page.locator('.report-directory-link').nth(7);
    const target = await movedLink.getAttribute('href');
    await movedLink.click();
    await page.waitForFunction(target => document.querySelector(target).classList.contains('is-current'), target);

    // A separate browser session can open a direct URL and update this list.
    const otherContext = await browser.newContext();
    const direct = await otherContext.newPage();
    await direct.goto(base + secondUrl);
    await returnToList('visibilitychange');
    await waitFirst(second);
    assert.deepEqual((await order()).slice(0, 2), [second, first]);
    await syncDirectory();

    await direct.goto(base + firstUrl);
    await returnToList('pageshow');
    await waitFirst(first);
    await direct.goto(base + secondUrl);
    await returnToList();
    await waitFirst(second);
    await direct.goto(base + firstUrl);
    await direct.reload();
    await returnToList();
    await waitFirst(first);
    assert.deepEqual(await order(), [first, second, ...original.filter(id => id !== first && id !== second)]);
    await page.reload();
    await waitFirst(first);
    await syncDirectory();

    await page.goto(base + '/reports');
    await waitFirst(first);
    assert.equal(await page.getByLabel('排序', {exact: true}).inputValue(), 'opened_desc');
    await page.getByLabel('排序', {exact: true}).selectOption('added_desc');
    await page.waitForURL(url => url.searchParams.get('sort') === 'added_desc');
    assert.deepEqual(await order(), original);
    await page.getByLabel('排序', {exact: true}).selectOption('opened_desc');
    await page.waitForURL(url => url.searchParams.get('sort') === 'opened_desc');
    await waitFirst(first);
    assert.deepEqual(errors, []);
    await otherContext.close();
    console.log('Recent opens: popup, direct URL, cross-browser persistence, refresh, return events, directory order, retained selection and recent-open default passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
