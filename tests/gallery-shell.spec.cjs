/* Run with Node + Playwright. Requests are fulfilled locally: no library writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery', 'ui-review');
fs.mkdirSync(artifacts, { recursive: true });
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace('</head>',
  '<link rel="stylesheet" href="/static/gallery-shell.css"><script defer src="/static/gallery-shell.js"></script><script defer src="/static/integration-settings.js"></script></head>');
const tags = [
  { name: 'aoi', zh_name: '現有繪師：青井', category: 'artist', count: 48, aliases: ['aoi', 'aoi_alt'] },
  { name: 'haru', zh_name: '春野', category: 'artist', count: 32 },
  { name: 'landscape', zh_name: '風景', category: 'general', count: 24 },
  { name: 'night', zh_name: '夜景', category: 'general', count: 16 },
];
const pictures = Array.from({ length: 96 }, (_, i) => ({
  id: i + 1, filename: `風景習作_${String(i + 1).padStart(3, '0')}.jpg`, rel_path: `風景/風景習作_${i + 1}.jpg`,
  folder: i % 3 ? '青井' : '', mtime: 1792454400, media_type: 'image',
  artists: [tags[i % 2]], characters: [], copyrights: [], preview_tags: [tags[2]],
  suggested_folder: '', rating: 'safe', ai_tagged: i % 2 === 0,
}));
const status = { running: false, model_loaded: false, status_message: 'AI 模型已卸載', processed_count: 0 };
const palettes = [['#293d5c', '#92aec0', '#506c81'], ['#45354e', '#cf9c9a', '#876480'], ['#224e51', '#a0bfa7', '#517f75'], ['#615044', '#d3ba9a', '#9f886d']];
function illustration(id) {
  const [dark, light, middle] = palettes[id % palettes.length];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="750" viewBox="0 0 600 750"><defs><linearGradient id="sky" x2=".4" y2="1"><stop stop-color="${dark}"/><stop offset="1" stop-color="${light}"/></linearGradient></defs><path fill="url(#sky)" d="M0 0h600v750H0z"/><circle cx="${180 + id % 4 * 65}" cy="195" r="65" fill="${light}" opacity=".85"/><path d="M0 550 175 240 400 600 600 350v400H0Z" fill="${middle}"/><path d="M0 650 250 420 600 650v100H0Z" fill="${dark}"/><path d="m175 240-55 95 55-20 52 48Z" fill="${light}" opacity=".75"/><path d="M0 690q140-100 300 0t300 0v60H0" fill="${light}" opacity=".3"/><text x="30" y="715" font-size="14" fill="${light}" font-family="sans-serif" letter-spacing="4">LANDSCAPE STUDY ${String(id).padStart(2, '0')}</text></svg>`;
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const errors = [], posts = [], queries = [];
    let failImages = false;
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      if (request.method() !== 'GET') { posts.push(pathname); return route.fulfill({ status: 405, body: '{}' }); }
      if (pathname === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (pathname.startsWith('/static/')) return route.fulfill({
        contentType: pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, pathname), 'utf8'),
      });
      if (/^\/api\/(thumb|raw)\//.test(pathname)) return route.fulfill({ contentType: 'image/svg+xml', body: illustration(Number(pathname.split('/').pop())) });
      let data = {};
      if (pathname === '/api/stats') data = { total_images: 96, root_unorganized: 32, folder_organized: 64, ai_tagged: 48, suggested_folder_count: 0, ai_status: status };
      else if (pathname === '/api/folders') data = [{ name: '青井', count: 48 }, { name: '春野', count: 16 }];
      else if (pathname === '/api/tags') {
        const items = tags.filter(tag => ['all', 'recent', tag.category].includes(url.searchParams.get('category')));
        data = {items, next_offset:items.length, has_more:false};
      }
      else if (pathname === '/api/runtime') data = { ai: status, reverse: status, schedule: { running_job: false, config: { enabled: false, schedule_times: [], source_dirs: [], history_logs: [] } }, model: { busy: false }, active_job: {}, jobs: [] };
      else if (pathname === '/api/ai/status' || pathname === '/api/reverse_search/status') data = status;
      else if (pathname === '/api/schedule/status') data = { running_job: false, status_message: '待機中', config: { enabled: false, schedule_times: ['03:00'], source_dirs: [], history_logs: [] } };
      else if (pathname === '/api/images') {
        queries.push(Object.fromEntries(url.searchParams));
        if (failImages) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
        let items = pictures;
        const q = url.searchParams.get('q');
        if (q === 'no-results') items = [];
        if (url.searchParams.get('folder') === '__ROOT__') items = items.filter(item => !item.folder);
        if (url.searchParams.get('ai_status') === 'tagged') items = items.filter(item => item.ai_tagged);
        const size = Number(url.searchParams.get('page_size') || 60), page = Number(url.searchParams.get('page') || 1);
        data = { total: items.length, items: items.slice((page - 1) * size, page * size), page, page_size: size };
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForFunction(() => document.querySelectorAll('.card').length > 0 && document.querySelector('#shell-load-status').hidden);
    assert.equal(await page.locator('#shell-library-title').textContent(), '全部圖片');
    assert.equal(queries[0].compact, 'true', 'The gallery requests compact card summaries');
    assert.equal(await page.locator('.card .pill.artist').count(), 60);
    assert.equal(await page.locator('.card .pill.general, .card .pill.character, .card .pill.copyright').count(), 0);
    assert.equal(await page.locator('.shell-nav .stat-pill').count(), 5);
    assert.equal(await page.locator('#ai-banner').isVisible(), false, 'Idle model must not show 0% / pause');
    assert.equal(await page.locator('.cat-pill.active[data-cat]').getAttribute('data-cat'), 'recent');
    assert.equal(await page.locator('#sel-sort').inputValue(), 'mtime_desc');
    assert.equal(queries[0].include_tags, '最近');
    const artistRow = page.locator('#sidebar-tag-list .tag-item').filter({ hasText: '青井' });
    assert.equal(await artistRow.count(), 1);
    assert.equal(await artistRow.locator('.tag-label-zh').textContent(), '青井');
    assert.equal(await artistRow.locator('.tag-label-en').count(), 0);
    assert.ok(!(await page.locator('#sidebar-tag-list').textContent()).includes('現有繪師'));
    await page.evaluate(() => toggleIncludeTag('aoi_alt'));
    await page.waitForFunction(() => document.querySelector('#sidebar-tag-list .tag-toggle').getAttribute('aria-pressed') === 'true');
    await page.waitForFunction(() => !document.querySelector('#image-grid .loading-overlay'));
    assert.equal(await page.locator('#active-filters-bar .filter-chip').filter({ hasText: '青井' }).count(), 1);
    await artistRow.getByRole('button').click();
    assert.deepEqual(await page.evaluate(() => state.includeTags), ['最近']);
    assert.equal(queries[0].sort, 'mtime_desc');
    assert.equal(await page.locator('.filter-summary').isVisible(), true, 'Show the default recent filter');
    await page.locator('#btn-clear-tags').click();
    await page.waitForFunction(() => document.querySelector('#shell-load-status').hidden);
    assert.equal(await page.locator('.filter-summary').isVisible(), false, 'Hide empty conditions row');
    assert.equal(await page.locator('.shell-sidebar').evaluate(el => el.inert), false);
    assert.equal(await page.locator('.sidebar-tabs [role="tab"]').count(), 3);
    assert.equal(await page.locator('#tab-btn-tags').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#panel-tags').isVisible(), true);
    assert.equal(await page.locator('#panel-library').isVisible(), false, 'Library options are hidden until their tab is selected');
    const tagList = await page.locator('#sidebar-tag-list').boundingBox();
    const tagsPanel = await page.locator('#panel-tags').boundingBox();
    const sidebarBox = await page.locator('.shell-sidebar').boundingBox();
    assert.ok(tagsPanel.y - sidebarBox.y < 60, 'The tab panels start at the top of the sidebar');
    assert.ok(tagList.height >= 600, `Tag browsing gets the freed sidebar height: ${tagList.height}`);
    const desktopGallery = await page.locator('#gallery-scroll').boundingBox();
    assert.ok(desktopGallery.y < 170, `Desktop toolbar leaves the gallery near the top: ${desktopGallery.y}`);
    assert.ok(desktopGallery.height >= 780, `Gallery keeps at least 78% of the 1000px viewport: ${desktopGallery.height}`);
    await page.screenshot({ path: path.join(artifacts, 'desktop.png') });

    await page.evaluate(() => updateAiBanner({ running: true, total_target: 10, processed_count: 2, status_message: '正在分析' }, true));
    assert.equal(await page.locator('#ai-banner button').isVisible(), true);
    await page.evaluate(() => updateAiBanner({ running: false, processed_count: 100, status_message: '完成' }, false));
    assert.equal(await page.locator('#ai-banner button').isVisible(), false);
    await page.evaluate(() => updateAiBanner({ running: true, status_message: '正在執行排程自動處理 — 整體進度：20%' }, true));
    assert.equal(await page.locator('#ai-banner button').isVisible(), false);
    await page.evaluate(() => updateAiBanner({ running: false, processed_count: 0 }));
    assert.equal(await page.locator('#ai-banner').isVisible(), false);

    const requestsBeforeTabs = queries.length;
    await page.locator('#tab-btn-tags').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#tab-btn-folders').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#panel-folders').isVisible(), true);
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#tab-btn-library').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#panel-library').isVisible(), true);
    assert.equal(await page.locator('#panel-tags').isVisible(), false);
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#tab-btn-tags').getAttribute('aria-selected'), 'true');
    await page.keyboard.press('End');
    assert.equal(await page.locator('#tab-btn-library').getAttribute('aria-selected'), 'true');
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#tab-btn-tags').getAttribute('aria-selected'), 'true');
    assert.equal(queries.length, requestsBeforeTabs, 'Switching tabs alone does not change filters or page');
    await page.locator('#tab-btn-library').click();
    assert.equal(await page.locator('.shell-nav .stat-pill:visible').count(), 5);
    await page.locator('.shell-sidebar').screenshot({ path: path.join(artifacts, 'desktop-library-tab.png') });
    await page.locator('[data-scope="root"]').focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#shell-result-count').textContent.includes('32'));
    assert.equal(await page.locator('#shell-library-title').textContent(), '待整理');
    const requestsAfterRoot = queries.length;
    await page.locator('#tab-btn-tags').click();
    assert.equal(await page.locator('#panel-tags').isVisible(), true);
    assert.equal(await page.locator('#shell-library-title').textContent(), '待整理', 'Changing sidebar tabs preserves the chosen collection');
    await page.locator('#tab-btn-folders').click();
    assert.equal(await page.locator('#panel-folders').isVisible(), true);
    assert.equal(queries.length, requestsAfterRoot);
    await page.locator('#tab-btn-library').click();
    await page.locator('[data-scope="ai"]').click();
    await page.waitForFunction(() => document.querySelector('#shell-result-count').textContent.includes('48'));
    assert.equal(queries.at(-1).folder, '__ALL__');
    await page.locator('[data-scope="all"]').click();
    await page.waitForFunction(() => document.querySelector('#shell-result-count').textContent.includes('96'));
    assert.equal(queries.at(-1).ai_status, 'all');
    await page.locator('#tab-btn-tags').click();

    await page.locator('[data-density="compact"]').click();
    const compactColumns = await page.locator('#image-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
    await page.reload();
    await page.waitForSelector('.card');
    assert.equal(await page.locator('body').getAttribute('data-gallery-density'), 'compact');
    await page.locator('[data-density="comfortable"]').click();
    const comfortableColumns = await page.locator('#image-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
    assert.ok(compactColumns > comfortableColumns, 'Density changes the number of columns');

    await page.locator('#shell-management summary').focus();
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.locator('#shell-management').getAttribute('open'), '');
    assert.ok((await page.evaluate(() => document.activeElement.textContent)).includes('批次改名'));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#shell-management').getAttribute('open'), null);

    await page.locator('body').click({ position: { x: 500, y: 100 } });
    await page.keyboard.press('/');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main-search');
    await page.locator('#main-search').fill('no-results');
    await page.waitForSelector('#shell-reset-empty');
    await page.locator('#shell-reset-empty').click();
    await page.waitForSelector('.card');
    await page.locator('.card-check').first().focus();
    await page.keyboard.press('Space');
    assert.equal(await page.locator('.card-check').first().getAttribute('aria-checked'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('card-check')), true, 'Selection preserves keyboard focus');
    await page.getByRole('button', { name: '取消選取' }).click();

    failImages = true;
    await page.locator('#sel-sort').selectOption('mtime_desc');
    await page.waitForSelector('#shell-retry');
    failImages = false;
    await page.locator('#shell-retry').click();
    await page.waitForSelector('.card');

    await page.locator('#btn-settings').click();
    assert.equal(await page.locator('#schedule-modal').isVisible(), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#schedule-modal').isVisible(), false);

    const widths = [2507, 1920, 1440, 1280, 1100, 1024, 900, 850, 801, 800, 768, 650, 521, 390, 320];
    for (const width of widths) {
      await page.setViewportSize({ width, height: width < 800 ? 844 : 1000 });
      await page.waitForTimeout(220);
      const bounds = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth, search: document.querySelector('#main-search').getBoundingClientRect().width }));
      assert.ok(bounds.content <= bounds.width, `No horizontal overflow at ${width}px: ${JSON.stringify(bounds)}`);
      assert.ok(bounds.search > 120, `Search remains usable at ${width}px`);
      const toolbar = await page.evaluate(() => {
        const rect = selector => {
          const box = document.querySelector(selector).getBoundingClientRect();
          return { top: box.top, bottom: box.bottom, right: box.right, center: (box.top + box.bottom) / 2 };
        };
        return { controls: ['.shell-search-field', '#sel-sort', '.shell-toolbar-actions'].map(rect), edge: rect('.filter-toolbar').right };
      });
      assert.ok(toolbar.controls.every(box => box.right <= toolbar.edge), `All toolbar controls remain visible at ${width}px`);
      if (width >= 1024) {
        const centers = toolbar.controls.map(box => box.center);
        assert.ok(Math.max(...centers) - Math.min(...centers) < 2, `Search, sorting and actions share one row at ${width}px`);
      }
      if (width <= 650) {
        const actionTops = toolbar.controls.slice(1).map(box => box.top);
        assert.ok(Math.max(...actionTops) - Math.min(...actionTops) < 2, `Mobile sorting and actions share one row at ${width}px`);
      }
      if (width <= 800) {
        assert.equal(await page.locator('.shell-sidebar').evaluate(el => el.inert), true);
        await page.locator('#shell-open-filters').click();
        await page.waitForFunction(() => Math.abs(document.querySelector('.shell-sidebar').getBoundingClientRect().left) < 1);
        assert.equal(await page.locator('.shell-sidebar').evaluate(el => el.inert), false);
        assert.equal(await page.locator('main').evaluate(el => el.inert), true);
        await page.locator('#tab-btn-tags').focus();
        await page.keyboard.press('End');
        assert.equal(await page.locator('#panel-library').isVisible(), true, 'Drawer allows keyboard switching to library filters');
        await page.keyboard.press('Home');
        assert.equal(await page.locator('#panel-tags').isVisible(), true);
        await page.locator('#shell-apply-filters').focus();
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'shell-close-filters');
        await page.keyboard.press('Escape');
        await page.waitForFunction(() => document.querySelector('.shell-sidebar').getBoundingClientRect().right < 1);
        assert.equal(await page.locator('main').evaluate(el => el.inert), false);
        assert.equal(await page.evaluate(() => document.activeElement.id), 'shell-open-filters');
      }
      if (width === 390) {
        await page.screenshot({ path: path.join(artifacts, 'mobile.png') });
        await page.locator('#shell-open-filters').click();
        await page.waitForFunction(() => Math.abs(document.querySelector('.shell-sidebar').getBoundingClientRect().left) < 1);
        await page.screenshot({ path: path.join(artifacts, 'mobile-filters.png') });
        await page.locator('#shell-apply-filters').click();
      }
    }
    await page.setViewportSize({ width: 320, height: 568 });
    await page.locator('#shell-open-filters').click();
    await page.waitForFunction(() => Math.abs(document.querySelector('.shell-sidebar').getBoundingClientRect().left) < 1);
    const shortTags = await page.locator('#sidebar-tag-list').boundingBox();
    assert.ok(shortTags.height >= 100, 'Short drawers retain usable tag browsing space');
    await page.locator('#shell-apply-filters').click();
    assert.deepEqual(errors, [], 'No browser JavaScript errors');
    assert.deepEqual(posts, [], 'Verification must not mutate the library');
    console.log(JSON.stringify({ result: 'PASS', widths, desktopGallery, imageRequests: queries.length, screenshots: artifacts }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
