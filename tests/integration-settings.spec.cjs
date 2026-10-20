/* All API calls are mocked. Verify MCP clients, live policy, jobs and mobile layout. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace('</head>',
  '<link rel="stylesheet" href="/static/gallery-shell.css"><script defer src="/static/gallery-shell.js"></script><script defer src="/static/integration-settings.js"></script></head>');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], mutations = [], calls = [], toolCalls = [];
    const toolCatalog = [
      { name: 'gallery_get_overview', label: '圖庫總覽', description: '讀取圖庫統計。', writes: false, enabled: true, example: {}, inputSchema: { type: 'object', properties: {} } },
      { name: 'gallery_batch_tag_or_move', label: '標籤與搬移', description: '批次標籤與搬移。', writes: true, enabled: true, example: { include_tags: ['landscape'] }, inputSchema: { type: 'object', required: ['include_tags'], properties: { include_tags: { type: 'array' } } } },
      { name: 'gallery_scan_library', label: '掃描圖庫', description: '掃描圖庫並保留人工標籤。', writes: true, enabled: true, background: true, example: {}, inputSchema: { type: 'object', properties: {} } },
      { name: 'gallery_cancel_job', label: '要求停止任務', description: '停止目前作業。', writes: true, enabled: true, exclusive: false, example: { job_id: '0'.repeat(32) }, inputSchema: { type: 'object', properties: { job_id: { type: 'string' } } } }
    ];
    let completedTool = { id: 'a'.repeat(32), name: 'gallery_get_overview', status: 'succeeded', source: 'settings', started_at: 1792454400, duration_ms: 12, result: { total_images: 42 }, error: '' };
    let config = { mcp_enabled: true, mcp_read_only: false, disabled_tools: [], sessions: [], tools: toolCatalog.map(({ name, label, writes }) => ({ name, label, writes })) };
    const ai = { running: false, model_loaded: false, disk_cached: false, status_message: '待機中' };
    const schedule = { running_job: false, config: { enabled: false, schedule_times: [], source_dirs: [], history_logs: [] } };
    let runtime = { ai, reverse: { running: false }, schedule, active_job: {}, model: { busy: false }, jobs: [], process_id: 100 };
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      calls.push(endpoint);
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {};
      if (endpoint === '/api/settings/integrations') {
        if (request.method() === 'POST') {
          const value = request.postDataJSON(); mutations.push(value);
          config = { ...config, ...value };
        }
        data = config;
      } else if (endpoint === '/api/settings/mcp/test') data = { ok: true, message: '本機握手成功' };
      else if (endpoint === '/api/settings/mcp/client_config') data = { mcpServers: { 'gallery': { command: 'python', args: ['mcp_server.py'], ...(url.searchParams.get('client') === 'antigravity' ? { disabled: false, disabledTools: ['gallery_scan_library'] } : { trust: false, includeTools: ['gallery_get_overview'] }) } } };
      else if (endpoint === '/api/settings/mcp/tools') data = { tools: toolCatalog.map(tool => ({ ...tool, enabled: config.mcp_enabled && !(config.mcp_read_only && tool.writes) })) };
      else if (endpoint.endsWith('/run') && endpoint.startsWith('/api/settings/mcp/tools/')) {
        toolCalls.push(request.postDataJSON()); data = { id: completedTool.id, status: 'running' };
      } else if (endpoint === '/api/settings/mcp/runs') data = { runs: toolCalls.length ? [completedTool] : [] };
      else if (endpoint.startsWith('/api/settings/mcp/runs/')) data = { run: completedTool };
      else if (endpoint.startsWith('/api/jobs/') && endpoint.endsWith('/stop')) data = { status: 'stopping', message: '已要求停止' };
      else if (endpoint === '/api/runtime') data = runtime;
      else if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/folders' || endpoint === '/api/tags') data = [];
      else if (endpoint === '/api/images') data = { items: [], total: 0 };
      else if (endpoint === '/api/schedule/status') data = schedule;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery-settings.test/'); await page.waitForSelector('#shell-reset-empty');
    await page.evaluate(() => openSettings('mcp'));
    await page.waitForSelector('#mcp-tool-list input', { state: 'attached' });
    await page.getByText('工具權限', { exact: true }).click();
    assert.equal(await page.locator('#settings-mcp').isVisible(), true);
    await page.locator('#mcp-readonly').check();
    assert.equal(await page.locator('[data-name="gallery_batch_tag_or_move"]').isDisabled(), true);
    assert.equal(await page.locator('[data-name="gallery_get_overview"]').isEnabled(), true);
    await page.locator('#mcp-save').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    assert.equal(mutations[0].mcp_read_only, true); assert.ok(!('api_key' in mutations[0]));
    await page.locator('#mcp-test').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('握手成功'));
    await page.locator('#mcp-export').click(); await page.waitForSelector('#mcp-client-config:visible');
    assert.equal(JSON.parse(await page.locator('#mcp-client-config').inputValue()).mcpServers['gallery'].disabled, false);
    assert.ok((await page.locator('#mcp-export-note').textContent()).includes('mcp_config.json'));
    await page.locator('#mcp-client-target').selectOption('gemini_cli');
    assert.equal(await page.locator('#mcp-client-config').isHidden(), true);
    await page.locator('#mcp-export').click();
    await page.waitForSelector('#mcp-client-config:visible');
    assert.equal(JSON.parse(await page.locator('#mcp-client-config').inputValue()).mcpServers['gallery'].trust, false);
    assert.ok((await page.locator('#mcp-export-note').textContent()).includes('settings.json'));
    assert.equal(await page.locator('#settings-tab-gemini').count(), 0);
    assert.equal(await page.locator('#gemini-key').count(), 0);
    await page.locator('#mcp-tool-workbench > summary').click();
    await page.waitForSelector('#mcp-tool-select option', { state: 'attached' });
    await page.locator('#mcp-tool-arguments').fill('invalid-json'); await page.locator('#mcp-tool-run').click();
    assert.equal(toolCalls.length, 0);
    assert.ok((await page.locator('#mcp-tool-feedback').textContent()).includes('JSON'));
    await page.locator('#mcp-tool-example').click(); await page.locator('#mcp-tool-run').click();
    await page.waitForFunction(() => document.querySelector('#mcp-tool-result').textContent.includes('42'));
    assert.deepEqual(toolCalls[0], { arguments: {} });
    await page.locator('#mcp-tool-select').selectOption('gallery_batch_tag_or_move');
    assert.equal(await page.locator('#mcp-tool-run').isDisabled(), true);
    assert.ok((await page.locator('#mcp-tool-effect').textContent()).includes('停用'));
    await page.locator('#mcp-tool-select').selectOption('gallery_get_overview');
    completedTool = { ...completedTool, status: 'failed', result: null, error: '工具失敗（模擬）' };
    await page.locator('#mcp-tool-run').click();
    await page.waitForFunction(() => document.querySelector('#mcp-tool-feedback').textContent.includes('工具失敗（模擬）'));
    assert.equal(await page.locator('#mcp-tool-feedback').evaluate(element => element.classList.contains('error')), true);
    assert.ok((await page.locator('#mcp-tool-history').textContent()).includes('失敗'));
    await page.locator('#mcp-readonly').uncheck(); await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    await page.locator('#mcp-tool-select').selectOption('gallery_scan_library');
    assert.ok((await page.locator('#mcp-tool-effect').textContent()).includes('任務 ID'));
    completedTool = { ...completedTool, id: 'b'.repeat(32), name: 'gallery_scan_library', status: 'running', job_id: 'c'.repeat(32), error: '', result: null };
    await page.locator('#mcp-tool-example').click(); await page.locator('#mcp-tool-run').click();
    await page.waitForSelector('#mcp-tool-stop:visible');
    assert.equal(await page.locator('#mcp-tool-run').isDisabled(), true);
    await page.locator('#mcp-tool-stop').click();
    assert.ok(calls.includes(`/api/jobs/${'c'.repeat(32)}/stop`));
    completedTool = { ...completedTool, status: 'cancelled', error: '使用者要求停止' };
    await page.waitForFunction(() => document.querySelector('#mcp-tool-feedback').textContent.includes('已停止'));
    await page.locator('#mcp-tool-select').selectOption('gallery_cancel_job');
    assert.equal(await page.locator('#mcp-tool-run').textContent(), '送出停止要求');
    await page.locator('#mcp-tool-select').selectOption('gallery_get_overview');
    const job = { id: 'external', pid: 200, name: 'Windows 排程', stage: 'AI 分析', processed_count: 2, total_target: 10, status: 'running' };
    runtime = { ...runtime, active_job: job, jobs: [job] };
    await page.evaluate(() => checkAiStatus());
    assert.ok((await page.locator('#ai-banner-msg').textContent()).includes('Windows 排程'));
    await page.locator('#settings-tab-tasks').click();
    assert.ok((await page.locator('#tasks-current').textContent()).includes('Windows 排程'));
    await page.evaluate(() => GalleryApp.refreshRuntime());
    runtime = { ...runtime, active_job: {}, jobs: [{ ...job, status: 'failed', last_error: '磁碟暫時不可用' }] };
    await page.evaluate(() => checkAiStatus());
    assert.ok((await page.locator('#ai-banner-msg').textContent()).includes('磁碟暫時不可用'));
    assert.equal(await page.locator('#ai-banner-pct').textContent(), '未完成');
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 900 }); await page.evaluate(() => switchSettingsTab('mcp'));
      await page.waitForSelector('#mcp-tool-list input');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(overflow, false, `No page overflow at ${width}px`);
    }
    const directory = path.join(root, '.tag_gallery/ui-review'); fs.mkdirSync(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, 'settings-mcp-mobile.png'), fullPage: true });
    await page.locator('#mcp-tool-arguments').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: path.join(directory, 'settings-mcp-desktop.png'), fullPage: true });
    await page.locator('#mcp-tool-arguments').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-desktop.png'), fullPage: true });
    assert.deepEqual(errors, []); assert.equal(calls.filter(value => ['/api/ai/status', '/api/reverse_search/status'].includes(value)).length, 0);
    console.log(JSON.stringify({ result: 'PASS', mutations: mutations.length, toolCalls: toolCalls.length, browserErrors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
