/* Verify browser storage across real reloads without writing to the image library. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const key = 'twitter-tag-gallery.tag-filters.v1';
const tags = [
  {name:'最近', zh_name:'最近', category:'recent', count:2},
  {name:'landscape', zh_name:'風景', category:'general', count:2},
  {name:'night', zh_name:'夜景', category:'general', count:1},
];
const image = {id:1, filename:'landscape.jpg', rel_path:'landscape.jpg', folder:'', media_type:'image',
  mtime:123, artists:[], characters:[], copyrights:[], preview_tags:[], rating:'safe'};

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const queries = [], tagQueries = [], errors = [], writes = [];
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (request.method() !== 'GET') { writes.push(endpoint); return route.fulfill({status:405, body:'{}'}); }
      if (endpoint === '/') return route.fulfill({contentType:'text/html', body:html});
      if (endpoint.startsWith('/static/')) return route.fulfill({
        contentType:endpoint.endsWith('.css') ? 'text/css' : 'text/javascript',
        body:fs.readFileSync(path.join(root, endpoint), 'utf8')
      });
      if (/^\/api\/(thumb|raw)\//.test(endpoint)) return route.fulfill({contentType:'image/svg+xml',
        body:'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="teal"/></svg>'});
      const ai = {running:false, model_loaded:false, processed_count:0};
      let data = {};
      if (endpoint === '/api/images') {
        queries.push(Object.fromEntries(url.searchParams));
        data = {items:[image], total:1};
      } else if (endpoint === '/api/tags') {
        tagQueries.push(Object.fromEntries(url.searchParams));
        const category = url.searchParams.get('category');
        const items = tags.filter(tag => ['all','recent',tag.category].includes(category));
        data = {items, has_more:false, next_offset:items.length};
      } else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/stats') data = {total_images:1, root_unorganized:1, folder_organized:0,
        ai_tagged:0, suggested_folder_count:0, ai_status:ai};
      else if (endpoint === '/api/runtime') data = {ai, reverse:ai, schedule:{config:{}}, active_job:{}, jobs:[], model:{}};
      else if (endpoint.endsWith('/status')) data = ai;
      return route.fulfill({contentType:'application/json', body:JSON.stringify(data)});
    });
    let page = await context.newPage();
    const watchErrors = tab => tab.on('pageerror', error => errors.push(error.message));
    watchErrors(page);
    const ready = async () => {
      await page.waitForSelector('.card');
      await page.waitForFunction(() => document.querySelector('#shell-load-status').hidden && document.querySelector('#sidebar-tag-list').getAttribute('aria-busy') === 'false');
    };
    const toggle = name => page.locator(`#sidebar-tag-list .tag-toggle[onclick="toggleIncludeTag('${name}')"]`);
    await page.goto('http://gallery.test/');
    await ready();
    assert.equal(queries[0].include_tags, '最近');
    await page.locator('#btn-clear-tags').click();
    await page.locator('[data-cat="general"]').click();
    await toggle('landscape').click();
    await toggle('night').click();
    await ready();
    await page.evaluate(() => { state.page = 7; });

    let firstQuery = queries.length;
    await page.reload();
    await ready();
    assert.equal(queries[firstQuery].include_tags, 'landscape,night', 'The first request already uses the saved selection');
    assert.equal(queries[firstQuery].page, '1', 'Reload starts at the first page');
    assert.equal(tagQueries.at(-1).category, 'general');
    assert.equal(await page.locator('.cat-pill.active').getAttribute('data-cat'), 'general');
    assert.equal(await toggle('landscape').getAttribute('aria-pressed'), 'true');
    assert.equal(await toggle('night').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#active-filters-bar .filter-chip').count(), 2);
    await page.getByRole('button', {name:'移除標籤：夜景', exact:true}).click();
    await ready();

    await page.close();
    page = await context.newPage();
    watchErrors(page);
    firstQuery = queries.length;
    await page.goto('http://gallery.test/');
    await ready();
    assert.equal(queries[firstQuery].include_tags, 'landscape', 'Reopening in the same browser retains the remaining tag');
    assert.equal(await toggle('night').getAttribute('aria-pressed'), 'false');

    for (const action of ['#btn-clear-tags', 'button[onclick="clearAllFilters()"]']) {
      if (!(await page.locator('#btn-clear-tags').isVisible())) await toggle('landscape').click();
      await page.locator(action).click();
      await ready();
      firstQuery = queries.length;
      await page.reload();
      await ready();
      assert.equal(queries[firstQuery].include_tags, '', `${action} remains cleared after reload`);
      assert.deepEqual(await page.evaluate(storageKey => JSON.parse(localStorage.getItem(storageKey)).includeTags, key), []);
      assert.equal(await page.locator('#active-filters-bar .filter-chip').count(), 0);
    }

    await page.evaluate(storageKey => localStorage.setItem(storageKey, 'broken json'), key);
    firstQuery = queries.length;
    await page.reload();
    await ready();
    assert.equal(queries[firstQuery].include_tags, '最近', 'Corrupted storage falls back to the default');
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    console.log(JSON.stringify({ok:true, restoredOnFirstRequest:true, categoryRestored:true, reopenRetainsTags:true,
      clearedSelectionPersists:true, malformedStorageHandled:true, browserErrors:errors.length, libraryWrites:writes.length}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
