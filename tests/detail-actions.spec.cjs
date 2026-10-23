/* Isolated detail-toolbar interactions; all media and API requests are mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery', 'ui-review');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const tags = [
  { name: 'aoi', zh_name: '青井', category: 'artist', source: 'manual' },
  { name: 'landscape', zh_name: '風景', category: 'general', source: 'ai', confidence: .98 },
  { name: 'night', zh_name: '夜景', category: 'general', source: 'ai', confidence: .94 },
];
const items = [1, 2].map(id => ({
  id, filename: `風景習作_${id}.jpg`, folder: '青井', rel_path: `青井/風景習作_${id}.jpg`,
  size_kb: 1053.6, media_type: 'image', mtime: 123, tags,
  artists: [tags[0]], characters: [], copyrights: [], preview_tags: tags.slice(1),
}));
const media = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><defs><linearGradient id="sky" x2=".5" y2="1"><stop stop-color="#29243e"/><stop offset="1" stop-color="#aa8798"/></linearGradient></defs><path fill="url(#sky)" d="M0 0h600v800H0z"/><circle cx="410" cy="195" r="65" fill="#dfc3bf"/><path d="M0 600 190 260 430 650 600 410v390H0Z" fill="#62536f"/><path d="M0 700 250 460 600 700v100H0Z" fill="#353348"/></svg>';

(async () => {
  fs.mkdirSync(artifacts, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], writes = [], rawRequests = [];
    let releaseAi, releaseReverse, holdDetail2 = false, releaseDetail2;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({
        contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript',
        body: fs.readFileSync(path.join(root, endpoint), 'utf8'),
      });
      if (/^\/api\/(raw|thumb)\//.test(endpoint)) {
        if (endpoint.startsWith('/api/raw/')) rawRequests.push(url.pathname + url.search);
        return route.fulfill({ contentType: 'image/svg+xml', body: media });
      }
      let data = {};
      const ai = { running: false, model_loaded: false, processed_count: 0, status_message: '待機中' };
      if (request.method() === 'POST') {
        writes.push(endpoint);
        if (endpoint === '/api/ai/single/1') await new Promise(resolve => { releaseAi = resolve; });
        else if (endpoint === '/api/reverse_search/single/1') {
          await new Promise(resolve => { releaseReverse = resolve; });
          data = { reverse_search_result: { found: false } };
        } else if (endpoint === '/api/decensor/preview/1') data = { ok: false, message: '此圖未偵測到馬賽克' };
        else if (endpoint !== '/api/open_explorer/1') throw Error(`Unexpected write: ${endpoint}`);
      } else if (endpoint === '/api/images') data = { items, total: items.length };
      else if (endpoint.startsWith('/api/image/')) {
        if (endpoint === '/api/image/2' && holdDetail2) await new Promise(resolve => { releaseDetail2 = resolve; });
        data = items.find(item => item.id === Number(endpoint.split('/').pop()));
      }
      else if (endpoint === '/api/stats') data = { total_images: 2, root_unorganized: 0, folder_organized: 2, ai_tagged: 2, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders') data = [{ name: '青井', count: 2 }];
      else if (endpoint === '/api/tags') data = {items:tags.map(tag => ({ ...tag, count: 2 })), next_offset:tags.length, has_more:false};
      else if (endpoint === '/api/runtime') data = { ai, reverse: ai, schedule: { config: {} }, active_job: {}, jobs: [], model: {} };
      else if (/\/status$/.test(endpoint)) data = ai;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.locator('#card-1 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '風景習作_1.jpg');
    assert.equal(await page.locator('#dt-add-input').count(), 0);
    assert.equal(await page.locator('#btn-single-decensor').isVisible(), true);
    assert.equal(await page.locator('#btn-single-unified').isVisible(), false);
    assert.equal(await page.locator('#btn-detail-explorer').isVisible(), true);
    assert.equal(await page.locator('#btn-delete-single').isVisible(), true);
    assert.equal(await page.locator('#dt-unified-hint').isVisible(), false);
    assert.equal(await page.locator('#btn-single-unified svg').count(), 1);
    const primary = await page.locator('#btn-detail-explorer').boundingBox();
    const decensor = await page.locator('#btn-single-decensor').boundingBox();
    const deletion = await page.locator('#btn-delete-single').boundingBox();
    const more = await page.locator('#btn-detail-more').boundingBox();
    assert.equal(primary.y, more.y);
    assert.equal(primary.height, more.height);
    assert.equal(decensor.y, primary.y);
    assert.equal(decensor.height, primary.height);
    assert.equal(deletion.y, primary.y);
    assert.equal(deletion.height, primary.height);
    await page.screenshot({ path: path.join(artifacts, 'detail-actions-desktop.png') });

    // Native disclosure, keyboard navigation, Escape and outside dismissal.
    await page.locator('#btn-detail-more').focus();
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-single-unified');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-single-unified');
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-single-unified');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#detail-more-actions').getAttribute('open'), null);
    assert.equal(await page.locator('#detail-modal').isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-detail-more');
    await page.locator('#btn-detail-more').click();
    await page.screenshot({ path: path.join(artifacts, 'detail-actions-expanded.png') });
    await page.locator('#dt-filename').click();
    assert.equal(await page.locator('#detail-more-actions').getAttribute('open'), null);
    assert.deepEqual(writes, []);

    // Frequent file actions work directly; AI tools remain available in More.
    await page.locator('#btn-detail-explorer').click();
    await page.waitForFunction(() => !document.querySelector('#detail-more-actions').open);
    assert.deepEqual(writes, ['/api/open_explorer/1']);
    await page.locator('#btn-single-decensor').click();
    await page.waitForFunction(() => document.querySelector('#dt-decensor-status').textContent.includes('未偵測到'));
    assert.equal(await page.locator('#dt-decensor-box').isVisible(), true);
    assert.equal(await page.locator('#dt-decensor-label').textContent(), '去馬賽克');
    assert.equal(await page.locator('#btn-single-decensor svg').count(), 1);
    await page.locator('#dt-decensor-box button').first().click();
    await page.locator('#btn-detail-more').click();
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#btn-delete-single').click();
    assert.equal(await page.locator('#detail-modal').isVisible(), true);
    assert.equal(await page.locator('#detail-more-actions').getAttribute('open'), null);
    assert.equal(writes.length, 2);

    // Busy labels stay short and preserve the toolbar geometry and icon.
    await page.locator('#btn-detail-more').click();
    await page.locator('#btn-single-unified').click();
    await page.waitForFunction(() => document.querySelector('#dt-unified-label').textContent === 'AI 辨識中…');
    assert.equal(await page.locator('#btn-single-unified').isDisabled(), true);
    assert.equal((await page.locator('#btn-detail-explorer').boundingBox()).height, primary.height);
    assert.equal(await page.locator('#dt-unified-hint').isVisible(), true);
    releaseAi();
    await page.waitForFunction(() => document.querySelector('#dt-unified-label').textContent === '搜圖校正中…');
    await page.waitForFunction(() => document.querySelector('#dt-unified-hint').textContent.includes('步驟 2/2'));
    releaseReverse();
    await page.waitForFunction(() => !document.querySelector('#btn-single-unified').disabled);
    assert.equal(await page.locator('#dt-unified-label').textContent(), '智慧標籤');
    assert.equal(await page.locator('#btn-single-unified svg').count(), 1);
    assert.equal(await page.locator('#dt-unified-hint').isVisible(), false);

    // Switching images resets the disclosure, keeps current media visible without flashing black, and preloads neighbors.
    assert.ok(rawRequests.includes('/api/raw/1'));
    assert.ok(rawRequests.includes('/api/raw/2'), 'Adjacent image is preloaded in background');
    assert.ok(rawRequests.every(req => !req.includes('?t=')), 'Normal detail loads use stable cacheable URLs');
    holdDetail2 = true;
    await page.locator('#btn-detail-more').click();
    await page.locator('.nav-arrow.next').click();
    await page.waitForFunction(() => typeof state !== 'undefined' && state.detailImageId === 2);
    assert.equal(await page.locator('#detail-more-actions').getAttribute('open'), null);
    assert.equal(await page.locator('#dt-main-img').getAttribute('src'), '/api/raw/1', 'Previous image stays visible while next image loads');
    assert.equal(await page.evaluate(() => {
      const img = document.getElementById('dt-main-img');
      return Boolean(img && img.complete && img.naturalWidth > 0);
    }), true);
    releaseDetail2();
    holdDetail2 = false;
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === '風景習作_2.jpg');
    assert.equal(await page.locator('#dt-main-img').getAttribute('src'), '/api/raw/2');
    assert.equal(await page.evaluate(() => {
      const img = document.getElementById('dt-main-img');
      return Boolean(img && img.complete && img.naturalWidth > 0);
    }), true);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#btn-detail-more').click();
    const card = await page.locator('#detail-modal .modal-card').boundingBox();
    assert.ok(card.x >= 0 && card.x + card.width <= 390);
    const menu = await page.locator('#detail-more-panel').boundingBox();
    assert.ok(menu.x >= card.x && menu.x + menu.width <= card.x + card.width);
    await page.screenshot({ path: path.join(artifacts, 'detail-actions-mobile.png') });
    await page.locator('#btn-delete-single').click({ trial: true });
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 320, height: 740 });
    const smallActions = await page.locator('.detail-actions').boundingBox();
    for (const id of ['btn-detail-explorer', 'btn-single-decensor', 'btn-delete-single', 'btn-detail-more']) {
      const box = await page.locator(`#${id}`).boundingBox();
      assert.equal(box.y, smallActions.y);
      assert.ok(box.x + box.width <= smallActions.x + smallActions.width + 1);
    }
    await page.locator('#btn-detail-more').click();
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#detail-modal').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('PASS: detail actions, keyboard and outside dismissal, original workflows, busy labels, desktop and mobile layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
