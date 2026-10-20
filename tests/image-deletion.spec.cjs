/* Real browser interactions; every request is mocked and no user file is deleted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const operation = 'delete_' + 'a'.repeat(32);
const original = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1, filename: `圖片_${i + 1}.png`, rel_path: `圖片_${i + 1}.png`, folder: '',
  media_type: 'image', mtime: 123, artists: [], characters: [], copyrights: [], preview_tags: [],
  tags: [], size_kb: 10
}));

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addInitScript(() => localStorage.setItem('twitter-tag-gallery.browse.v1', '{"pageSize":10}'));
    let items = [...original], deleted = [], failDelete = false;
    const writes = [], errors = [];
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      if (/^\/api\/(thumb|raw)\//.test(endpoint)) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="teal"/></svg>' });
      let data = {};
      const ai = { running: false, model_loaded: false, processed_count: 0 };
      if (request.method() === 'POST') {
        writes.push({ endpoint, payload: request.postDataJSON() });
        if (endpoint === '/api/images/delete') {
          if (failDelete) return route.fulfill({ status: 409, contentType: 'application/json', body: '{"detail":"檔案使用中"}' });
          const ids = request.postDataJSON().image_ids;
          deleted = items.filter(item => ids.includes(item.id));
          items = items.filter(item => !ids.includes(item.id));
          data = { deleted: deleted.length, deleted_ids: ids, operation_id: operation };
        } else if (endpoint === `/api/images/deletions/${operation}/restore`) {
          items.push(...deleted); items.sort((a, b) => a.id - b.id);
          data = { restored: deleted.length };
        } else throw Error(`Unexpected write: ${endpoint}`);
      } else if (endpoint === '/api/images') {
        const page = Number(url.searchParams.get('page')), size = Number(url.searchParams.get('page_size'));
        data = { items: items.slice((page - 1) * size, page * size), total: items.length };
      } else if (endpoint.startsWith('/api/image/')) data = items.find(item => item.id === Number(endpoint.split('/').pop()));
      else if (endpoint === '/api/stats') data = { total_images: items.length, root_unorganized: items.length, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders' || endpoint === '/api/tags') data = [];
      else if (endpoint === '/api/runtime') data = { ai, reverse: ai, schedule: { config: {} }, active_job: {}, jobs: [], model: {} };
      else if (/\/status$/.test(endpoint)) data = ai;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForFunction(() => document.querySelectorAll('.card').length === 10);

    // A selection survives paging. The confirmation explicitly includes off-page files.
    await page.locator('#card-1 .card-check').click();
    await page.locator('#btn-next').click();
    await page.waitForSelector('#card-11');
    await page.locator('#card-11 .card-check').click();
    page.once('dialog', async dialog => { assert.match(dialog.message(), /其他頁面選取的 1/); await dialog.dismiss(); });
    await page.locator('#btn-delete-selected').click();
    assert.equal(writes.length, 0);
    page.once('dialog', dialog => dialog.accept());
    await page.keyboard.press('Delete');
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已刪除 2'));
    assert.deepEqual(writes[0].payload.image_ids, [1, 11]);
    assert.equal(await page.locator('#batch-bar').isVisible(), false);
    assert.equal(await page.locator('#page-num-label').textContent(), '第 1 / 1 頁');

    // Undo survives a reload, restores the batch, and clears its persisted action.
    await page.reload();
    await page.locator('#btn-restore-deleted').click();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已復原 2'));
    assert.equal(items.length, 12);

    // Delete while typing is ignored. Detail always deletes one image even with a selection.
    await page.locator('#card-2 .card-check').click();
    await page.locator('#main-search').focus();
    await page.keyboard.press('Delete');
    assert.equal(writes.length, 2);
    await page.locator('#card-1 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_1.png');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#btn-delete-single').click();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已刪除 1'));
    assert.deepEqual(writes[2].payload.image_ids, [1]);
    assert.equal(await page.locator('#detail-modal').isVisible(), false);
    assert.equal(await page.locator('#batch-count').textContent(), '1');

    // Failure leaves the selection available for retry.
    failDelete = true;
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#btn-delete-selected').click();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.includes('檔案使用中'));
    assert.equal(await page.locator('#batch-count').textContent(), '1');
    assert.equal(await page.locator('#btn-delete-selected').isEnabled(), true);
    assert.deepEqual(errors, []);
    const artifacts = path.join(root, '.tag_gallery', 'ui-review');
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, 'image-deletion.png') });
    console.log('PASS: batch and single deletion, Delete key, cancel, cross-page selection, page clamp, persisted undo and retry');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
