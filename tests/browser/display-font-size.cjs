const assert = require('node:assert/strict');
const {chromium} = require('playwright');

const origin = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
const reportUrl = `${origin}/artifacts/2026-10-03/reports/paper-0/report.html`;
const fontSize = locator => locator.evaluate(node => parseFloat(getComputedStyle(node).fontSize));

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const context = await browser.newContext({viewport: {width: 1600, height: 900}});
    const page = await context.newPage();
    await page.goto(`${origin}/settings`);
    assert.equal(await page.locator('html').evaluate(node => getComputedStyle(node).zoom), '1');

    const settingsHeading = page.getByRole('heading', {name: '设置', exact: true});
    const initialFont = await fontSize(settingsHeading);
    const interfaceSize = page.getByRole('combobox', {name: '界面字号'});
    await interfaceSize.selectOption('120');
    assert.ok(await fontSize(settingsHeading) > initialFont);
    await page.reload();
    assert.equal(await interfaceSize.inputValue(), '120');
    await page.getByRole('button', {name: '恢复默认界面字号'}).click();
    assert.equal(await fontSize(settingsHeading), initialFont);
    await page.reload();
    assert.equal(await interfaceSize.inputValue(), '100');

    const report = await context.newPage();
    await report.goto(reportUrl);
    assert.equal(await report.locator('html').evaluate(node => getComputedStyle(node).zoom), '1');
    const reportSize = report.getByRole('combobox', {name: '正文字号'});
    assert.equal(await reportSize.inputValue(), '100');
    const prose = report.locator('.report-article p').first();
    const title = report.locator('.report-article h1');
    const table = report.locator('.report-article td').first();
    const formula = report.locator('.report-article .katex');
    await formula.waitFor();
    const brand = report.locator('.report-brand');
    const narrowBase = await fontSize(prose);
    await report.getByRole('button', {name: '收起论文边栏'}).click();
    await report.waitForFunction(size => parseFloat(getComputedStyle(document.querySelector('.report-article p')).fontSize) > size, narrowBase);
    const wideBase = await fontSize(prose);
    const wideTitle = await fontSize(title);
    const wideTable = await fontSize(table);
    const wideFormula = await fontSize(formula);
    const brandBase = await fontSize(brand);
    await reportSize.selectOption('120');
    assert.ok(await fontSize(prose) > wideBase);
    assert.ok(await fontSize(title) > wideTitle);
    assert.ok(await fontSize(table) > wideTable);
    assert.ok(await fontSize(formula) > wideFormula);
    assert.equal(await fontSize(brand), brandBase);

    const wideLarger = await fontSize(prose);
    await report.getByRole('button', {name: '阅读对话 / 提问选区'}).click();
    assert.ok(await fontSize(prose) < wideLarger);
    await report.getByRole('button', {name: '收起阅读助手'}).click();
    assert.equal(await fontSize(prose), wideLarger);

    await report.reload();
    assert.equal(await reportSize.inputValue(), '120');
    const largerProse = await fontSize(prose);
    await interfaceSize.selectOption('140');
    await report.waitForFunction(size => parseFloat(getComputedStyle(document.querySelector('.report-brand')).fontSize) > size, brandBase);
    assert.equal(await fontSize(prose), largerProse);

    await report.setViewportSize({width: 390, height: 844});
    assert.ok(await fontSize(prose) < largerProse);
    assert.equal(await report.locator('html').evaluate(node => node.scrollWidth <= node.clientWidth), true);
    const mobileLarger = await fontSize(prose);
    await report.getByRole('button', {name: '恢复默认正文字号'}).click();
    assert.ok(await fontSize(prose) < mobileLarger);
    assert.equal(await interfaceSize.inputValue(), '140');
    await report.reload();
    assert.equal(await reportSize.inputValue(), '100');
    console.log('PASS native page scale, independent persistent font sizes, responsive report text and reset');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
