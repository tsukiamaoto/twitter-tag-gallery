/* Mocked scan lifecycle: no gallery writes or external requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery', 'ui-review');

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    let phase = 'header', starts = 0;
    const errors = [], calls = [];
    page.on('pageerror', error => errors.push(error.message));
    const scan = () => ({scan_id: 'shared-scan', status: phase === 'failed' ? 'failed' : phase === 'done' ? 'completed' : 'running',
      elapsed_seconds: 12, error: phase === 'failed' ? '圖片暫時無法讀取，請重試' : undefined,
      progress: {stage: phase === 'header' ? '檢查尺寸與快取' : '建立候選圖像素索引',
        processed_count: phase === 'header' ? 250 : 60, total_target: phase === 'header' ? 1000 : 100,
        cached_images: 40, skipped_images: 750, exact_copies_skipped: 12},
      result: phase === 'done' ? {total_scanned: 1000, total_groups: 0, total_duplicate_images: 0, groups: [], all_delete_ids: []} : undefined});
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      calls.push(endpoint);
      if (endpoint === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8')});
      if (endpoint.startsWith('/static/')) return route.fulfill({contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8')});
      let data = {};
      if (endpoint === '/api/dedup/scans' && request.method() === 'POST') { starts++; data = scan(); }
      else if (endpoint === '/api/dedup/scans/latest') data = {scan: null};
      else if (endpoint === '/api/dedup/scans/shared-scan') data = scan();
      else if (request.method() !== 'GET') throw Error('Unexpected mutation: ' + endpoint);
      else if (endpoint === '/api/stats') data = {total_images: 1000, root_unorganized: 1000, folder_organized: 0,
        ai_tagged: 0, suggested_folder_count: 0, ai_status: {running: false}};
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = {items: [], has_more: false};
      else if (endpoint === '/api/images') data = {items: [], total: 0};
      else if (endpoint === '/api/runtime') data = {ai: {}, reverse: {}, model: {}, schedule: {config: {}}, active_job: {}, jobs: []};
      return route.fulfill({contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto('http://dedup-progress.test/');
    await page.evaluate(() => openDedupModal());
    await page.waitForFunction(() => document.querySelector('#dedup-progress-bar').value === 250);
    assert.equal(await page.locator('#btn-dedup-exec').isDisabled(), true);
    assert.equal(await page.locator('#dedup-preview-box').isVisible(), false);
    phase = 'pixels';
    await page.waitForFunction(() => document.querySelector('#dedup-progress-bar').value === 60);
    assert.match(await page.locator('#dedup-progress-detail').textContent(), /沿用像素快取 40 張/);
    fs.mkdirSync(artifacts, {recursive: true});
    await page.screenshot({path: path.join(artifacts, 'dedup-progress-desktop.png')});
    await page.setViewportSize({width: 390, height: 900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    assert.equal(await page.locator('#dedup-progress').isVisible(), true);
    await page.screenshot({path: path.join(artifacts, 'dedup-progress-mobile.png')});
    phase = 'done';
    await page.waitForFunction(() => document.querySelector('#dedup-progress').hidden);
    assert.equal(await page.locator('#dedup-preview-box').isVisible(), true);
    assert.match(await page.locator('#dedup-summary-bar').textContent(), /1,000/);
    await page.evaluate(() => closeDedupModal());
    phase = 'failed';
    await page.evaluate(() => openDedupModal());
    await page.waitForFunction(() => document.querySelector('#dedup-summary-bar').textContent.includes('請重試'));
    assert.equal(await page.locator('#btn-dedup-exec').isDisabled(), true);
    assert.equal(await page.locator('#btn-dedup-scan').isEnabled(), true);
    assert.equal(starts, 2);
    assert.equal(calls.includes('/api/dedup/preview'), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({result: 'PASS', starts, screenshots: artifacts}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
