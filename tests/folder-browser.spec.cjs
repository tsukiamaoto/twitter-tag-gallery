/* All library requests are fulfilled locally; no files or tags are changed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery', 'ui-review');
fs.mkdirSync(artifacts, { recursive: true });
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace('</head>',
  '<link rel="stylesheet" href="/static/gallery-shell.css"><script defer src="/static/gallery-shell.js"></script><script defer src="/static/integration-settings.js"></script></head>');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    let folders = [
      { name: 'Alpha 2', count: 12, last_updated: 1792454400 },
      { name: 'Alpha 10', count: 60, last_updated: 1704067200 },
      { name: 'Beta', count: 20, last_updated: 1735689600 },
      { name: "O'Brien & co", count: 30, last_updated: 1704067200 },
      { name: '青井', count: 50, last_updated: 1704067200 },
      ...Array.from({ length: 1842 }, (_, index) => ({ name: `Artist ${index + 1}`, count: index % 100 + 1, last_updated: 1700000000 + index })),
    ];
    let failFolders = false, includeMetadata = true;
    const errors = [], writes = [], images = [];
    const ai = { running: false, model_loaded: false, status_message: '待機中' };
    await context.route('**/*', route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (request.method() !== 'GET') { writes.push(endpoint); return route.fulfill({ status: 405, body: '{}' }); }
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {};
      if (endpoint === '/api/folders') {
        if (failFolders) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' });
        data = includeMetadata ? folders : folders.map(({ name, count }) => ({ name, count }));
      } else if (endpoint === '/api/stats') data = { total_images: 31042, root_unorganized: 0, folder_organized: 31042, ai_tagged: 100, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/tags') data = {items:[], next_offset:0, has_more:false};
      else if (endpoint === '/api/images') { images.push(Object.fromEntries(url.searchParams)); data = { items: [], total: 0 }; }
      else if (endpoint === '/api/ai/status' || endpoint === '/api/reverse_search/status') data = ai;
      else if (endpoint === '/api/schedule/status') data = { running_job: false, config: { source_dirs: [], history_logs: [] } };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForFunction(() => document.querySelector('#folder-total').textContent.includes('1,847'));
    await page.locator('#tab-btn-folders').click();
    assert.equal(await page.locator('#folder-all').getAttribute('open'), null);
    assert.equal(await page.locator('.folder-browse-row').count(), 0, 'The 1847-folder catalog is not expanded by default');
    assert.equal(await page.locator('#folder-pinned').isVisible(), true);
    assert.equal(await page.locator('#folder-recent').isVisible(), true);
    await page.locator('.shell-sidebar').screenshot({ path: path.join(artifacts, 'folder-browse-empty.png') });

    const beforeSearch = images.length;
    await page.locator('#folder-search-input').fill('ＡＬＰＨＡ');
    assert.deepEqual(await page.locator('#folder-search-results .folder-name').allTextContents(), ['Alpha 2', 'Alpha 10']);
    await page.locator('#folder-sort').selectOption('count');
    assert.deepEqual(await page.locator('#folder-search-results .folder-name').allTextContents(), ['Alpha 10', 'Alpha 2']);
    await page.locator('#folder-sort').selectOption('updated');
    assert.deepEqual(await page.locator('#folder-search-results .folder-name').allTextContents(), ['Alpha 2', 'Alpha 10']);
    assert.equal(await page.locator('#folder-search-results .folder-updated').count(), 2);
    assert.equal(images.length, beforeSearch, 'Searching and sorting folders do not query images');
    await page.getByRole('button', { name: '釘選：Alpha 2', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: '取消釘選：Alpha 2', exact: true }).getAttribute('aria-pressed'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('folder-pin')), true);
    await page.evaluate(() => { state.includeTags = ['landscape']; state.page = 4; });
    await page.getByRole('button', { name: '查看資料夾：Alpha 2', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#shell-library-title').textContent === 'Alpha 2');
    assert.equal(images.at(-1).folder, 'Alpha 2');
    assert.equal(images.at(-1).page, '1');
    assert.equal(images.at(-1).include_tags, 'landscape');
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('folder-open')), true);
    await page.locator('#folder-clear-search').click();
    assert.deepEqual(await page.locator('#folder-pinned .folder-name').allTextContents(), ['Alpha 2']);
    assert.equal(await page.locator('#folder-recent .folder-browse-row').count(), 0, 'Pinned entries are not duplicated in recent shortcuts');
    await page.locator('#folder-search-input').fill('beta');
    await page.getByRole('button', { name: '查看資料夾：Beta', exact: true }).click();
    await page.locator('#folder-clear-search').click();
    assert.deepEqual(await page.locator('#folder-recent .folder-name').allTextContents(), ['Beta']);
    await page.locator('.shell-sidebar').screenshot({ path: path.join(artifacts, 'folder-browse-shortcuts.png') });
    await page.getByRole('button', { name: '取消釘選：Alpha 2', exact: true }).click();
    assert.deepEqual(await page.locator('#folder-recent .folder-name').allTextContents(), ['Beta', 'Alpha 2']);
    await page.getByRole('button', { name: '釘選：Alpha 2', exact: true }).click();

    await page.reload();
    await page.waitForFunction(() => document.querySelector('#folder-total').textContent.includes('1,847'));
    await page.locator('#tab-btn-folders').click();
    assert.deepEqual(await page.locator('#folder-pinned .folder-name').allTextContents(), ['Alpha 2']);
    assert.deepEqual(await page.locator('#folder-recent .folder-name').allTextContents(), ['Beta']);
    assert.equal(await page.locator('#folder-sort').inputValue(), 'updated');
    assert.equal(await page.locator('#folder-all').getAttribute('open'), null);

    const beforeExpand = images.length;
    await page.locator('#folder-all > summary').click();
    await page.waitForFunction(() => document.querySelectorAll('#folder-all .folder-browse-row').length === 100);
    await page.locator('#folder-all .folder-load-more').click();
    assert.equal(await page.locator('#folder-all .folder-browse-row').count(), 200);
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('folder-open')), true);
    assert.equal(images.length, beforeExpand);
    await page.locator('#folder-all > summary').click();
    await page.waitForFunction(() => !document.querySelector('#folder-all').open);
    await page.locator('#folder-search-input').fill('artist');
    assert.equal(await page.locator('#folder-search-results .folder-browse-row').count(), 100);
    await page.locator('#folder-search-results .folder-load-more').click();
    assert.equal(await page.locator('#folder-search-results .folder-browse-row').count(), 200);
    await page.locator('#folder-sort').selectOption('name');
    assert.equal(await page.locator('#folder-search-results .folder-browse-row').count(), 100);
    await page.locator('#folder-search-input').fill("O'Brien &");
    assert.deepEqual(await page.locator('#folder-search-results .folder-name').allTextContents(), ["O'Brien & co"]);
    await page.locator('#folder-search-input').fill('no-such-folder');
    assert.equal(await page.locator('#folder-search-results .folder-browse-row').count(), 0);
    assert.ok((await page.locator('#folder-search-results').textContent()).includes('找不到符合'));
    await page.locator('#folder-clear-search').click();

    failFolders = true;
    await page.evaluate(() => loadFolders());
    assert.equal(await page.getByRole('button', { name: '重新讀取', exact: true }).count(), 1);
    assert.equal(await page.evaluate(() => state.folders.length), 1847);
    failFolders = false;
    await page.getByRole('button', { name: '重新讀取', exact: true }).click();
    await page.waitForSelector('#folder-pinned');
    assert.deepEqual(await page.locator('#folder-pinned .folder-name').allTextContents(), ['Alpha 2']);
    folders = folders.filter(folder => folder.name !== 'Alpha 2');
    await page.evaluate(() => loadFolders());
    assert.equal(await page.locator('#folder-pinned .folder-browse-row').count(), 0);
    assert.ok(!(await page.evaluate(() => JSON.parse(localStorage.getItem('gallery.folders.preferences')).pinned)).includes('Alpha 2'));

    await page.evaluate(() => { localStorage.setItem('gallery.folders.preferences', '{broken'); });
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#folder-total').textContent.includes('1,846'));
    await page.locator('#tab-btn-folders').click();
    assert.equal(await page.locator('#folder-sort').inputValue(), 'name');
    await page.evaluate(() => { Object.defineProperty(Storage.prototype, 'setItem', { configurable: true, value() { throw new Error('blocked'); } }); });
    await page.locator('#folder-search-input').fill('Beta');
    await page.getByRole('button', { name: '釘選：Beta', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: '取消釘選：Beta', exact: true }).getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#folder-preference-status').isVisible(), true);

    for (const width of [1024, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      if (width <= 800) {
        await page.locator('#shell-open-filters').click();
        await page.waitForFunction(() => Math.abs(document.querySelector('.shell-sidebar').getBoundingClientRect().left) < 1);
      }
      const geometry = await page.locator('#panel-folders').evaluate(panel => {
        const edge = panel.getBoundingClientRect().right;
        return [...panel.querySelectorAll('.folder-search-field, .folder-toolbar, .folder-browse-row')].every(element => element.getBoundingClientRect().right <= edge);
      });
      assert.ok(geometry, `Folder controls fit ${width}px`);
      if (width <= 800) await page.locator('#shell-apply-filters').click();
    }
    includeMetadata = false;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#folder-total').textContent.includes('1,846'));
    const legacyState = await page.evaluate(() => ({ optionDisabled: document.querySelector('#folder-sort [value="updated"]').disabled, sample: state.folders[0], hint: document.querySelector('#folder-total').textContent }));
    assert.equal(legacyState.optionDisabled, true, `Older API data falls back without inventing dates: ${JSON.stringify(legacyState)}`);
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    console.log(JSON.stringify({ result: 'PASS', folders: 1847, browserErrors: errors.length, imageRequests: images.length, writeRequests: writes.length, screenshots: artifacts }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
