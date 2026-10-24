/* Exercise only the artist MCP handoff UI with disposable browser state and mock APIs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace('</head>',
  '<link rel="stylesheet" href="/static/gallery-shell.css"><script defer src="/static/gallery-shell.js"></script><script defer src="/static/integration-settings.js"></script></head>');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const writes = [], errors = [];
    const ai = { running: false, model_loaded: false, status_message: '待機中' };
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {};
      if (endpoint === '/api/settings/artist-identity') data = { config: { enabled: false, mode: 'mcp' }, identities: 5, pending_count: 1,
        requests: [{ request_key: 'a'.repeat(64), artist_name: '待查畫師', profile_urls_json: '[]', status: 'pending_model', last_error: '' }] };
      else if (endpoint === '/api/settings/integrations') data = { mcp_enabled: true, mcp_read_only: false, tools: [], sessions: [] };
      else if (endpoint === '/api/settings/mcp/client_config') data = { mcpServers: { gallery: { command: 'python', args: ['mcp_server.py'], disabled: false, disabledTools: [] } } };
      else if (request.method() === 'POST') {
        writes.push({ endpoint, arguments: request.postDataJSON() });
        data = { id: 'b'.repeat(32), status: 'running' };
      } else if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = { items: [], has_more: false, next_offset: 0 };
      else if (endpoint === '/api/images') data = { items: [], total: 0 };
      else if (endpoint === '/api/runtime') data = { ai, jobs: [], schedule: { config: {}, running_job: false }, reverse: {}, model: {}, active_job: {} };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://artist-identity.test/');
    await page.evaluate(() => openSettings('mcp'));
    await page.waitForSelector('.mcp-list-card[data-server-id="gallery"]');
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    assert.equal(await page.locator('#mcp-client-target').count(), 0);
    await page.locator('#mcp-export').click();
    await page.waitForSelector('#mcp-client-config:visible');
    assert.equal(JSON.parse(await page.locator('#mcp-client-config').inputValue()).mcpServers.gallery.disabled, false);
    assert.ok((await page.locator('#mcp-export-note').textContent()).includes('mcp_config.json'));
    await page.locator('#mcp-modal-close').click();
    await page.evaluate(() => openSettings('artists'));
    await page.waitForFunction(() => document.querySelector('#artist-model-status').textContent.includes('5'));
    assert.ok((await page.locator('#artist-model-status').textContent()).includes('1 筆待補查'));
    assert.ok((await page.locator('#artist-model-requests').textContent()).includes('等待 MCP 模型補查'));
    assert.equal(await page.locator('#artist-model-enabled').count(), 0);
    assert.equal(await page.locator('#artist-model-executable').count(), 0);
    assert.ok((await page.locator('#artist-mcp-prompt').inputValue()).includes('gallery_confirm_artist_identity'));
    await page.getByRole('button', { name: '重查 Danbooru', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#artist-model-feedback').textContent.includes('圖庫不啟動模型'));
    assert.deepEqual(writes, [{ endpoint: '/api/settings/mcp/tools/gallery_resolve_artist_identity/run', arguments: { arguments: { artist_name: '待查畫師', profile_urls: [], force_refresh: true } } }]);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      if (width < 800) await page.evaluate(() => window.GalleryApp.runtime.setDrawer?.(false));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(root, `.tag_gallery/ui-review/artist-mcp-${width}.png`), fullPage: true, animations: 'disabled' });
    }
    assert.deepEqual(errors, []);
    console.log('PASS: artist MCP queue, handoff instructions, no CLI controls, Danbooru retry and responsive layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
