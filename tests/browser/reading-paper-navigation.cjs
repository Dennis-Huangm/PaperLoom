// Start tests/browser/serve_report_fixture.py before running this regression.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    for (const width of [1920, 1200, 900, 600, 390]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const session = {id: 'fixture', title: 'Fixture', paper: {title: 'Paper', arxiv_id: '2608.01977', version: 1}, messages: []};
      await page.route('**/api/reading/**', route => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith('/events')) return route.fulfill({contentType: 'text/event-stream', body: 'event: snapshot\ndata: ' + JSON.stringify(session) + '\n\n'});
        return route.fulfill({json: path.endsWith('/settings') ? {model: 'fixture'} : path.endsWith('/sessions') && route.request().method() === 'GET' ? [] : session});
      });
      await page.goto((process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771') + '/artifacts/2026-10-03/reports/paper-0/report.html');
      const toggle = page.locator('#report-papers-toggle');
      await page.locator('.reading-open').click();
      for (const dockWidth of [480, 700, 900]) {
        await page.evaluate(value => document.body.style.setProperty('--reading-width', value + 'px'), dockWidth);
        assert.equal(await toggle.evaluate(button => {
          const box = button.getBoundingClientRect();
          return button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
        }), true, `paper catalog button must be clickable with reading open at viewport ${width}, dock ${dockWidth}`);
        assert.equal(await toggle.evaluate(button => {
          const box = button.getBoundingClientRect();
          const brand = document.querySelector('#report-reading .reading-brand').getBoundingClientRect();
          return box.x >= 0 && box.right <= innerWidth && (box.x >= brand.right || box.y >= brand.bottom);
        }), true, 'catalog button must fit without overlapping the reading title');
      }
      await toggle.click();
      assert.equal(await page.locator('#report-reading').isHidden(), true);
      await page.locator('.report-paper-link').first().waitFor();
      assert.equal(await page.locator('#report-papers').isVisible(), true);
      await page.locator('#report-papers-close').click();
      await page.locator('.reading-open').click();
      await page.getByRole('button', {name: '收起阅读助手', exact: true}).click();
      assert.equal(await toggle.isVisible(), true);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS paper catalog remains clickable during reading, resizing, switching and closing at five viewport widths');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
