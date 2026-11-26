// Start serve_report_fixture.py before running this test.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width: 3440, height: 1080}});
    const origin = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
    const reportUrl = number => `${origin}/artifacts/2026-10-03/reports/paper-${number}/report.html`;
    await page.goto(reportUrl(0));
    const toggle = page.getByRole('button', {name: '阅读宽度', exact: true});
    const panel = page.locator('#report-width-panel');
    const width = page.getByRole('slider', {name: '阅读宽度', exact: true});
    const reset = panel.getByRole('button', {name: '恢复默认', exact: true});
    const title = page.locator('.report-content > h1');
    const prose = page.locator('.report-content > h2 + p').first();
    const metadata = page.locator('table.report-metadata');
    const matrix = page.locator('.report-content > table.comparison-matrix');
    const tableDetails = page.locator('details.report-table-details');
    const foldedMatrix = tableDetails.locator('table.comparison-matrix');
    const image = page.locator('.report-image').first();
    const size = () => prose.evaluate(node => getComputedStyle(node).fontSize);
    const readBox = locator => locator.boundingBox();

    assert.equal(await panel.isVisible(), false, 'the toolbar only shows the width button by default');
    const standard = await readBox(prose);
    assert.equal(standard.width, 1200, 'an ultrawide screen uses the standard reading width by default');
    await tableDetails.locator('summary').click();
    const toolbarHeight = (await readBox(page.locator('.report-topbar'))).height;
    await toggle.press('Enter');
    assert.equal(await panel.isVisible(), true);
    assert.equal((await readBox(page.locator('.report-topbar'))).height, toolbarHeight);
    assert.equal(await width.evaluate(node => document.activeElement === node), true);
    assert.equal(await width.inputValue(), '1200');
    assert.equal(await width.getAttribute('list'), null, 'the slider has no tick marks');
    assert.deepEqual(await panel.getByRole('button').allTextContents(), ['恢复默认']);
    assert.equal(await panel.locator('output').textContent(), '1200 px');
    assert.ok((await readBox(panel)).width <= 220, 'the desktop panel stays compact');
    assert.equal(await width.getAttribute('aria-valuetext'), '1200 像素，默认');
    assert.equal((await readBox(title)).x, standard.x);
    assert.equal((await readBox(metadata)).x, standard.x);
    assert.equal((await readBox(metadata)).width, standard.width);
    const article = await readBox(page.locator('.report-article'));
    assert.ok(Math.abs(standard.x + standard.width / 2 - article.x - article.width / 2) < 1);
    async function assertTablesFit() {
      const column = await readBox(prose);
      const table = await readBox(matrix);
      const details = await readBox(tableDetails);
      const foldedTable = await readBox(foldedMatrix);
      assert.equal(table.x, column.x, 'a wide table starts at the prose margin');
      assert.equal(table.width, column.width, 'a wide table follows the reading width');
      assert.ok(details.x >= column.x && details.x + details.width <= column.x + column.width);
      assert.ok(foldedTable.x >= details.x && foldedTable.x + foldedTable.width <= details.x + details.width);
      assert.equal(await matrix.evaluate(node => node.scrollWidth > node.clientWidth), true);
      assert.equal(await foldedMatrix.evaluate(node => node.scrollWidth > node.clientWidth), true);
    }
    await assertTablesFit();
    assert.ok((await readBox(image)).width > standard.width);
    assert.equal((await readBox(page.locator('.report-image + em').first())).width, standard.width);

    const fontSize = await size();
    await width.press('Home');
    assert.equal((await readBox(prose)).width, 600);
    await assertTablesFit();
    await reset.click();
    assert.equal((await readBox(prose)).width, 1200);
    await width.press('ArrowRight');
    assert.equal((await readBox(prose)).width, 1220);
    await width.press('End');
    assert.equal((await readBox(prose)).width, 2560);

    const track = await readBox(width);
    assert.ok(track.width > track.height);
    const resetBox = await readBox(reset);
    assert.ok(resetBox.y >= track.y + track.height && Math.abs(resetBox.x + resetBox.width - track.x - track.width) < 1);
    await page.mouse.move(track.x + track.width - 8, track.y + track.height / 2);
    await page.mouse.down();
    await page.mouse.move(track.x + track.width * .75, track.y + track.height / 2, {steps: 8});
    const draggedWidth = Number(await width.inputValue());
    assert.ok(draggedWidth > 1600 && draggedWidth < 2560);
    assert.equal((await readBox(prose)).width, draggedWidth, 'the text reflows during the drag');
    await page.mouse.up();
    assert.equal(await size(), fontSize);
    await width.press('Escape');
    assert.equal(await panel.isVisible(), false);
    assert.equal(await toggle.evaluate(node => document.activeElement === node), true);
    await toggle.click();
    await title.click();
    assert.equal(await panel.isVisible(), false);
    await page.goto(reportUrl(1));
    await toggle.click();
    assert.equal(await width.inputValue(), String(draggedWidth));

    await page.setViewportSize({width: 390, height: 844});
    assert.ok((await readBox(prose)).width < 390);
    assert.equal(await page.locator('html').evaluate(node => node.scrollWidth <= node.clientWidth), true);
    await tableDetails.locator('summary').click();
    await assertTablesFit();
    await toggle.click();
    await reset.click();
    assert.equal(await page.evaluate(() => localStorage.getItem('paperloom.display.report-width')), null);
    await page.setViewportSize({width: 3440, height: 1080});
    assert.equal((await readBox(prose)).width, standard.width);
    await page.setViewportSize({width: 320, height: 844});
    await page.evaluate(() => localStorage.setItem('paperloom.display.interface-size', '140'));
    await page.reload();
    await toggle.click();
    const mobilePanel = await readBox(panel);
    const mobileToggle = await readBox(toggle);
    assert.ok(mobilePanel.x >= 0 && mobilePanel.x + mobilePanel.width <= 320);
    assert.ok(mobilePanel.y >= mobileToggle.y + mobileToggle.height);
    assert.equal(await page.locator('html').evaluate(node => node.scrollWidth <= node.clientWidth), true);
    await toggle.click();
    assert.equal(await panel.isVisible(), false);
    console.log('PASS compact width popover, horizontal drag, keyboard dismissal, persistent width and mobile layout');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
