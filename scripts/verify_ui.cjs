/* End-to-end UI check against a running local server; no API mocking. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const report = JSON.parse(fs.readFileSync(path.join(root, 'artifacts/run-report.json'), 'utf8'));
  const base = process.env.DEMO_URL || 'http://127.0.0.1:8000';
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('dialog', d => d.accept());
  await page.addInitScript(data => {
    if (!localStorage.getItem('mbses:project')) {
      localStorage.setItem('mbses:project', data.project_id);
      localStorage.setItem('mbses:task:' + data.project_id, data.reliability_task_id);
    }
  }, report);
  await page.goto(base);
  await page.locator('#taskStatus').filter({ hasText: '已确认' }).waitFor();
  await page.locator('#modelMeta').filter({ hasText: '18' }).waitFor();
  assert.match(await page.locator('#calculation').innerText(), /0\.913816/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: path.join(root, 'artifacts/desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: path.join(root, 'artifacts/mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  await page.locator('#newProject').click();
  await page.locator('#projectName').fill('浏览器全流程验证');
  await page.locator('#createProject').click();
  await page.locator('#projectDialog').waitFor({ state: 'hidden' });
  await page.locator('#file').setInputFiles(report.source);
  await page.locator('#upload').click();
  await page.locator('#uploadStatus').filter({ hasText: '1/1 成功' }).waitFor();
  await page.locator('#requirement').fill('提取电动牙刷的自动刷牙、更换刷头、质量要求、防水需求，以及刷牙、充电、更换刷头三个用例和模块结构。');
  await page.locator('#generate').click();
  await page.locator('#taskStatus').filter({ hasText: '待人工审核' }).waitFor();
  assert.equal(await page.locator('#confirm').isDisabled(), true);
  const draft = JSON.parse(await page.locator('#draft').inputValue());
  assert.equal(draft.model_elements.length, 11);
  // Exercise the real invalid -> corrected -> validated -> confirmed review path.
  const invalid = structuredClone(draft);
  invalid.sysml_relationships[0].target = 'not-an-element';
  await page.locator('#draft').fill(JSON.stringify(invalid, null, 2));
  await page.locator('#validate').click();
  await page.locator('#validationMessages').filter({ hasText: 'ENDPOINT_MISSING' }).waitFor();
  assert.equal(await page.locator('#confirm').isDisabled(), true);
  await page.locator('#draft').fill(JSON.stringify(draft, null, 2));
  await page.locator('#validate').click();
  await page.waitForFunction(() => !document.getElementById('confirm').disabled);
  await page.locator('#confirm').click();
  await page.locator('#taskStatus').filter({ hasText: '已确认' }).waitFor();
  await page.locator('#modelMeta').filter({ hasText: '11' }).waitFor();
  await page.locator('#tab-elements').click();
  assert.equal(await page.locator('#elementsBody tr').count(), 11);
  await page.locator('#tab-relations').click();
  assert.equal(await page.locator('#relationsBody tr').count(), 4);
  await page.locator('#tab-graph').click();
  assert.equal(await page.locator('#panel-graph svg rect').count(), 22);
  const taskId = await page.locator('#taskId').inputValue();
  await page.reload();
  await page.locator('#taskStatus').filter({ hasText: '已确认' }).waitFor();
  assert.equal(await page.locator('#taskId').inputValue(), taskId);
  await page.screenshot({ path: path.join(root, 'artifacts/review-confirmed.png'), fullPage: true });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(root, 'artifacts/ui-report.json'), JSON.stringify({
    passed: true, widths: [1440, 390], horizontal_overflow: false, browser_errors: errors,
    flow: 'real DOCX upload -> generate -> invalid draft rejected -> corrected -> validate -> confirm -> reload',
    elements: 11, relations: 4, task_id: taskId
  }, null, 2));
  console.log('UI PASS: real DOCX, full review path, desktop/mobile layout, persistence; 0 page errors.');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
