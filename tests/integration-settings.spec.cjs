/* All API calls are mocked. Verify MCP card list, pop-up modal, custom MCP CRUD, live policy, jobs and mobile layout. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], mutations = [], calls = [];
    const toolCatalog = [
      { name: 'gallery_get_overview', label: '圖庫總覽', description: '讀取圖庫統計。', writes: false, enabled: true, example: {}, inputSchema: { type: 'object', properties: {} } },
      { name: 'gallery_batch_tag_or_move', label: '標籤與搬移', description: '批次標籤與搬移。', writes: true, enabled: true, background: true, example: { include_tags: ['landscape'] }, inputSchema: { type: 'object', required: ['include_tags'], properties: { include_tags: { type: 'array' } } } },
      { name: 'gallery_scan_library', label: '掃描圖庫', description: '掃描圖庫並保留人工標籤。', writes: true, enabled: true, background: true, example: {}, inputSchema: { type: 'object', properties: {} } },
      { name: 'gallery_cancel_job', label: '要求停止任務', description: '停止目前作業。', writes: true, enabled: true, exclusive: false, example: { job_id: '0'.repeat(32) }, inputSchema: { type: 'object', properties: { job_id: { type: 'string' } } } }
    ];
    let config = { mcp_enabled: true, mcp_read_only: false, disabled_tools: [], sessions: [], tools: toolCatalog.map(({ name, label, writes }) => ({ name, label, writes })) };
    const artistConfig = { mode: 'mcp' };
    const ai = { running: false, model_loaded: false, disk_cached: false, status_message: '待機中' };
    const schedule = { running_job: false, config: { enabled: false, schedule_times: [], source_dirs: [], history_logs: [] } };
    let runtime = { ai, reverse: { running: false }, schedule, active_job: {}, model: { busy: false }, jobs: [], process_id: 100 };
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      calls.push(endpoint);
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {};
      if (endpoint === '/api/settings/artist-identity') {
        data = { config: artistConfig, identities: 5, pending_count: 0, requests: [] };
      } else if (endpoint === '/api/settings/integrations') {
        if (request.method() === 'POST') {
          const value = request.postDataJSON(); mutations.push(value);
          config = { ...config, ...value };
        }
        data = config;
      } else if (endpoint === '/api/settings/mcp/servers' && request.method() === 'POST') {
        const value = request.postDataJSON(); mutations.push(value);
        data = { ok: true, server: { ...value, server_name: value.id, builtin: false }, message: `已儲存 MCP 服務「${value.name}」。` };
      } else if (endpoint.startsWith('/api/settings/mcp/servers/') && request.method() === 'DELETE') {
        const deletedId = decodeURIComponent(endpoint.split('/').pop());
        mutations.push({ deleted: deletedId });
        data = { ok: true, deleted: deletedId, message: `已刪除 MCP 服務「${deletedId}」。` };
      } else if (endpoint === '/api/settings/mcp/test') data = { ok: true, message: '本機握手成功' };
      else if (endpoint === '/api/settings/mcp/client_config') data = { mcpServers: { 'gallery': { command: 'python', args: ['mcp_server.py'], disabled: false, disabledTools: ['gallery_scan_library'] } } };
      else if (endpoint === '/api/settings/mcp/tools') data = { tools: toolCatalog.map(tool => ({ ...tool, enabled: config.mcp_enabled && !(config.mcp_read_only && tool.writes) && !config.disabled_tools.includes(tool.name) })) };
      else if (endpoint === '/api/runtime') data = runtime;
      else if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = {items:[], next_offset:0, has_more:false};
      else if (endpoint === '/api/images') data = { items: [], total: 0 };
      else if (endpoint === '/api/schedule/status') data = schedule;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery-settings.test/'); await page.waitForSelector('#shell-reset-empty');
    await page.evaluate(() => openSettings('mcp'));
    await page.waitForSelector('#mcp-tool-list input', { state: 'attached' });
    await page.waitForSelector('.mcp-list-card[data-server-id="gallery"]');
    assert.equal(await page.locator('#settings-mcp').isVisible(), true);
    assert.equal(await page.locator('#mcp-modal').isHidden(), true, 'MCP modal starts closed on card list view');
    assert.equal(await page.locator('.mcp-list-card').count(), 2, 'Both built-in Antigravity MCP cards are displayed');
    assert.ok((await page.locator('.mcp-list-card[data-server-id="gallery"] .mcp-list-card-title').textContent()).includes('Antigravity 圖庫 MCP'));
    assert.ok((await page.locator('.mcp-list-card[data-server-id="gallerypub"] .mcp-list-card-title').textContent()).includes('Antigravity 背景查證 MCP'));

    // Clicking the Antigravity gallery card opens the pop-up configuration modal.
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    await page.locator('#mcp-readonly').check();
    await page.locator('#mcp-save').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    assert.deepEqual(mutations[0], { mcp_enabled: true, mcp_read_only: true });
    assert.equal(await page.locator('[data-name="gallery_batch_tag_or_move"]').isDisabled(), true);
    assert.equal(await page.locator('[data-name="gallery_get_overview"]').isEnabled(), true);
    await page.locator('#mcp-test').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('握手成功'));
    await page.locator('#mcp-export').click(); await page.waitForSelector('#mcp-client-config:visible');
    assert.equal(JSON.parse(await page.locator('#mcp-client-config').inputValue()).mcpServers['gallery'].disabled, false);
    assert.ok((await page.locator('#mcp-export-note').textContent()).includes('mcp_config.json'));
    assert.equal(await page.locator('#mcp-client-target').count(), 0);
    assert.equal(await page.locator('#settings-tab-gemini').count(), 0);
    assert.equal(await page.locator('#gemini-key').count(), 0);
    await page.locator('#mcp-modal-close').click();
    assert.equal(await page.locator('#mcp-modal').isHidden(), true);

    // Adding and deleting a custom MCP card from the UI.
    await page.locator('#mcp-card-new').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    await page.locator('#mcp-server-id').fill('comfyui-mcp');
    await page.locator('#mcp-server-name').fill('ComfyUI 外部工作流 MCP');
    await page.locator('#mcp-server-transport').selectOption('http');
    await page.locator('#mcp-server-url').fill('http://127.0.0.1:8188/mcp');
    await page.locator('#mcp-modal-custom-details > summary').click();
    await page.locator('#mcp-server-custom-config').fill('workflow=inpaint\ntimeout_ms=15000');
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelectorAll('.mcp-list-card').length === 3);
    assert.equal(mutations[1].id, 'comfyui-mcp');
    assert.equal(mutations[1].transport, 'http');
    assert.equal(mutations[1].url, 'http://127.0.0.1:8188/mcp');
    assert.deepEqual(mutations[1].custom_config, { workflow: 'inpaint', timeout_ms: '15000' });
    await page.locator('#mcp-delete').click();
    await page.waitForFunction(() => document.querySelectorAll('.mcp-list-card').length === 2);
    assert.equal(await page.locator('#mcp-modal').isHidden(), true);

    await page.locator('#settings-tab-tools').click();
    await page.waitForSelector('#agent-tool-catalog .agent-tool-row');
    assert.equal(await page.locator('#mcp-tool-workbench').count(), 0);
    assert.equal(await page.locator('#mcp-tool-arguments').count(), 0);
    assert.equal(await page.locator('#mcp-tool-history').count(), 0);
    assert.equal(await page.locator('#agent-prompt-schema').count(), 0);
    assert.equal(await page.locator('#agent-tool-catalog .agent-tool-row').count(), 4);
    assert.equal(await page.locator('.agent-tool-row[data-tool="gallery_get_overview"] .agent-tool-row-content').textContent(), '讀取圖庫統計。');
    assert.ok((await page.locator('.agent-tool-row[data-tool="gallery_batch_tag_or_move"] .agent-tool-row-meta').textContent()).includes('已停用'));
    await page.locator('.agent-tool-row[data-tool="gallery_get_overview"]').click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), true);
    assert.ok((await page.locator('#agent-prompt-name').textContent()).includes('gallery_get_overview'));
    assert.equal(await page.locator('#agent-usage-prompt').inputValue(), '讀取圖庫統計。');
    await page.locator('#agent-prompt-close').click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);
    await page.locator('#settings-tab-mcp').click();
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    await page.locator('#mcp-readonly').uncheck(); await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    await page.locator('#mcp-modal-close').click();
    await page.locator('#settings-tab-tools').click();
    await page.waitForFunction(() => document.querySelector('.agent-tool-row[data-tool="gallery_batch_tag_or_move"] .agent-tool-row-meta').textContent.includes('已啟用'));
    const job = { id: 'external', pid: 200, name: 'Windows 排程', stage: 'AI 分析', processed_count: 2, total_target: 10, status: 'running' };
    runtime = { ...runtime, active_job: job, jobs: [job] };
    await page.evaluate(() => checkAiStatus());
    assert.ok((await page.locator('#ai-banner-msg').textContent()).includes('Windows 排程'));
    await page.evaluate(() => openSettings('tasks'));
    assert.ok((await page.locator('#tasks-current').textContent()).includes('Windows 排程'));
    await page.evaluate(() => GalleryApp.refreshRuntime());
    runtime = { ...runtime, active_job: {}, jobs: [{ ...job, status: 'failed', last_error: '磁碟暫時不可用' }] };
    await page.evaluate(() => checkAiStatus());
    assert.ok((await page.locator('#ai-banner-msg').textContent()).includes('磁碟暫時不可用'));
    assert.equal(await page.locator('#ai-banner-pct').textContent(), '未完成');
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 900 }); await page.evaluate(() => switchSettingsTab('mcp'));
      await page.waitForSelector('#mcp-tool-list input', { state: 'attached' });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(overflow, false, `No page overflow at ${width}px`);
    }
    const directory = path.join(root, '.tag_gallery/ui-review'); fs.mkdirSync(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, 'settings-mcp-mobile.png'), fullPage: true });
    await page.evaluate(() => switchSettingsTab('tools'));
    await page.locator('#agent-tool-catalog').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => switchSettingsTab('mcp'));
    await page.screenshot({ path: path.join(directory, 'settings-mcp-desktop.png'), fullPage: true });
    await page.evaluate(() => switchSettingsTab('tools'));
    await page.locator('#agent-tool-catalog').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-desktop.png'), fullPage: true });
    await page.evaluate(() => switchSettingsTab('artists'));
    await page.waitForFunction(() => document.querySelector('#artist-model-status').textContent.includes('5'));
    assert.ok((await page.locator('#artist-model-status').textContent()).includes('MCP 客戶端'));
    assert.equal(await page.locator('#artist-model-enabled').count(), 0);
    assert.equal(await page.locator('#artist-model-executable').count(), 0);
    assert.ok((await page.locator('#artist-mcp-prompt').inputValue()).includes('gallery_confirm_artist_identity'));
    await page.screenshot({ path: path.join(directory, 'artist-identity-settings-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.screenshot({ path: path.join(directory, 'artist-identity-settings-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []); assert.equal(calls.filter(value => ['/api/ai/status', '/api/reverse_search/status'].includes(value)).length, 0);
    console.log(JSON.stringify({ result: 'PASS', mutations: mutations.length, browserErrors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
