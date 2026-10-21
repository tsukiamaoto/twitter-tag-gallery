/* Real browser interactions; every request is mocked and no user file is deleted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
let operation = null, operationSequence = 0;
const original = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1, filename: `圖片_${i + 1}.png`, rel_path: `圖片_${i + 1}.png`, folder: '',
  media_type: 'image', mtime: 123, artists: [], characters: [], copyrights: [], preview_tags: [],
  tags: [], size_kb: 10
}));

async function clickDetailDelete(page) {
  if (!await page.locator('#btn-delete-single').isVisible()) await page.locator('#btn-detail-more').click();
  await page.locator('#btn-delete-single').click();
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addInitScript(() => localStorage.setItem('twitter-tag-gallery.browse.v1', '{"pageSize":10}'));
    let items = [...original], deleted = [], failDelete = false;
    let holdRefresh = false, failImages = false;
    let onHeldRefresh = null;
    const pendingRefresh = [];
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
          operation = 'delete_' + (++operationSequence).toString(16).padStart(32, '0');
          deleted = items.filter(item => ids.includes(item.id));
          items = items.filter(item => !ids.includes(item.id));
          data = { deleted: deleted.length, deleted_ids: ids, operation_id: operation,
            restorable: deleted.filter(item => !item.file_missing).length,
            missing_count: deleted.filter(item => item.file_missing).length,
            already_deleted: deleted.length === 0 };
        } else if (endpoint === `/api/images/deletions/${operation}/restore`) {
          items.push(...deleted); items.sort((a, b) => a.id - b.id);
          data = { restored: deleted.length };
        } else if (endpoint === '/api/decensor/apply/2') {
          items.unshift({ ...original[1], id: 99, filename: 'copy.webp', rel_path: 'copy.webp' });
          data = { ok: true, image_id: 99, message: '已另存新圖' };
        } else throw Error(`Unexpected write: ${endpoint}`);
      } else if (endpoint === '/api/images') {
        const page = Number(url.searchParams.get('page')), size = Number(url.searchParams.get('page_size'));
        data = { items: items.slice((page - 1) * size, page * size), total: items.length };
        if (failImages) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
        if (holdRefresh) { pendingRefresh.push({route, data}); onHeldRefresh?.(); return; }
      } else if (endpoint.startsWith('/api/image/')) data = items.find(item => item.id === Number(endpoint.split('/').pop()));
      else if (endpoint === '/api/stats') data = { total_images: items.length, root_unorganized: items.length, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = {items:[], next_offset:0, has_more:false};
      else if (endpoint === '/api/runtime') data = { ai, reverse: ai, schedule: { config: {} }, active_job: {}, jobs: [], model: {} };
      else if (/\/status$/.test(endpoint)) data = ai;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForFunction(() => document.querySelectorAll('.card').length === 10);
    assert.equal(await page.locator('#btn-restore-last-deleted').evaluate(el => el.hidden), true);

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

    // Dismissing only hides this batch's notice, including after a reload.
    await page.reload();
    await page.locator('#btn-dismiss-delete-notice').click();
    assert.equal(await page.locator('#file-delete-notice').isVisible(), false);
    assert.equal(writes.length, 1, 'Dismissal makes no file request');
    assert.equal(await page.evaluate(() => localStorage.getItem('twitter-tag-gallery.last-deletion')), operation);
    await page.reload();
    await page.waitForSelector('#card-2');
    assert.equal(await page.locator('#file-delete-notice').isVisible(), false);

    // The management menu retains undo after dismissal and a reload.
    await page.locator('#shell-management > summary').click();
    await page.locator('#btn-restore-last-deleted').click();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已復原 2'));
    assert.equal(items.length, 12);
    assert.equal(writes[1].endpoint, `/api/images/deletions/${operation}/restore`);
    assert.equal(await page.locator('#btn-restore-last-deleted').evaluate(el => el.hidden), true);
    assert.equal(await page.evaluate(() => localStorage.getItem('twitter-tag-gallery.last-deletion')), null);
    assert.equal(await page.evaluate(() => localStorage.getItem('twitter-tag-gallery.last-deletion.dismissed')), null);

    // Delete while typing is ignored. Detail always deletes one image even with a selection.
    await page.locator('#card-2 .card-check').click();
    await page.locator('#main-search').focus();
    await page.keyboard.press('Delete');
    assert.equal(writes.length, 2);
    await page.locator('#card-1 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_1.png');
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已刪除 1'));
    assert.deepEqual(writes[2].payload.image_ids, [1]);
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_2.png');
    assert.equal(await page.locator('#detail-modal').isVisible(), true);
    assert.equal(await page.locator('#batch-count').textContent(), '1');
    await page.keyboard.press('Escape');

    // Failure leaves the selection available for retry.
    failDelete = true;
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#btn-delete-selected').click();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.includes('檔案使用中'));
    assert.equal(await page.locator('#batch-count').textContent(), '1');
    assert.equal(await page.locator('#btn-delete-selected').isEnabled(), true);

    // A saved copy changes both the displayed ID and list order. Delete must
    // use that ID, never the old list index from the original image.
    failDelete = false;
    await page.locator('#card-2 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_2.png');
    await page.evaluate(() => applySingleDecensor('new_copy'));
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === 'copy.webp');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#detail-media-container').click();
    await page.keyboard.press('Delete');
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_2.png');
    assert.deepEqual(writes.at(-1).payload.image_ids, [99]);
    assert.ok(items.some(item => item.id === 2), 'Original remains after deleting its displayed copy');
    await page.keyboard.press('Escape');

    // An already absent file renders a message; deleting it removes its index.
    items.find(item => item.id === 3).file_missing = true;
    await page.locator('#card-3 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#detail-media-container').textContent.includes('檔案不存在'));
    assert.equal(await page.locator('#dt-main-img').count(), 0);
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.includes('其中 1 個檔案原已不存在'));
    assert.deepEqual(writes.at(-1).payload.image_ids, [3]);
    assert.equal(await page.locator('#card-3').count(), 0);
    await page.keyboard.press('Escape');

    // Removal is visible before a slow refill. Neither an older request nor a
    // stale refill may put the deleted card back onto the screen.
    holdRefresh = true;
    await page.evaluate(() => { window.pendingOldList = loadImages(); });
    await page.waitForFunction(() => document.querySelector('#gallery-scroll').getAttribute('aria-busy') === 'true');
    await page.locator('#card-4 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_4.png');
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.startsWith('已刪除 1'));
    assert.equal(await page.locator('#card-4').count(), 0, 'Card is removed while refill is still pending');
    assert.equal(pendingRefresh.length, 2);
    const retryReady = new Promise(resolve => { onHeldRefresh = () => { if (pendingRefresh.length === 3) resolve(); }; });
    await pendingRefresh[0].route.fulfill({contentType:'application/json', body:JSON.stringify(pendingRefresh[0].data)});
    await pendingRefresh[1].route.fulfill({contentType:'application/json', body:JSON.stringify(pendingRefresh[0].data)});
    await retryReady;
    onHeldRefresh = null;
    assert.equal(await page.locator('#card-4').count(), 0, 'Stale refill cannot resurrect a removed card');
    holdRefresh = false;
    for (const pending of pendingRefresh.slice(2)) await pending.route.fulfill({contentType:'application/json', body:JSON.stringify(pending.data)});
    await page.waitForFunction(() => document.querySelector('#gallery-scroll').getAttribute('aria-busy') === 'false');
    await page.keyboard.press('Escape');

    // A failed refill keeps the already updated list visible.
    failImages = true;
    await page.locator('#card-5 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_5.png');
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.includes('其餘圖片暫時無法補載'));
    assert.equal(await page.locator('#card-5').count(), 0);
    assert.ok(await page.locator('.card').count() > 0, 'Other cards remain visible after failed refill');
    assert.equal(await page.locator('#shell-retry').count(), 0);
    await page.keyboard.press('Escape');

    // Deleting the final card of a page fills that slot from the next page.
    failImages = false;
    items = original.map(item => ({...item, file_missing:false}));
    await page.reload();
    await page.locator('#card-10 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_10.png');
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_11.png');
    assert.equal(await page.locator('#card-10').count(), 0);
    assert.equal(await page.locator('#card-11').count(), 1);

    // An expired viewer still advances when its record was already removed.
    items = items.filter(item => item.id !== 11);
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_12.png');
    assert.equal(await page.locator('#card-11').count(), 0);

    // There is no next image at the end, so keep browsing the previous one.
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_9.png');
    await page.keyboard.press('Escape');
    await page.locator('#btn-dismiss-delete-notice').click();
    const dismissedOperation = operation;
    items = [{...original[0], file_missing:false}];
    await page.reload();
    await page.waitForSelector('#card-1');
    assert.equal(await page.locator('#file-delete-notice').isVisible(), false);
    await page.locator('#card-1 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '圖片_1.png');
    page.once('dialog', dialog => dialog.accept());
    await clickDetailDelete(page);
    await page.waitForFunction(() => !document.querySelector('#detail-modal').classList.contains('open'));
    assert.equal(await page.locator('.card').count(), 0);
    assert.notEqual(operation, dismissedOperation);
    assert.equal(await page.locator('#file-delete-notice').isVisible(), true, 'A new deletion has its own notice');
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#file-delete-status').textContent.includes('上次刪除'));
    assert.equal(await page.locator('#file-delete-notice').isVisible(), true, 'An older dismissal does not hide a new batch');
    assert.deepEqual(errors, []);
    const artifacts = path.join(root, '.tag_gallery', 'ui-review');
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, 'image-deletion.png') });
    console.log('PASS: batch and single deletion, Delete key, cancel, cross-page selection, page clamp, persisted dismissal, menu undo, new batch notice and retry');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
