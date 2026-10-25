/* All API calls are mocked. Verify MCP card list, pop-up modal, custom MCP CRUD, live policy, jobs and mobile layout. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const directory = path.join(root, '.tag_gallery/ui-review');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    fs.mkdirSync(directory, { recursive: true });
    const errors = [], mutations = [], calls = [];
    const toolCatalog = [
      { name: 'gallery_get_overview', label: '圖庫總覽', description: '讀取圖庫統計。', writes: false, enabled: true, example: {}, inputSchema: { type: 'object', properties: {} } },
      { name: 'gallery_batch_tag_or_move', label: '標籤與搬移', description: '批次標籤與搬移。', writes: true, enabled: true, background: true, example: { include_tags: ['landscape'] }, inputSchema: { type: 'object', required: ['include_tags'], properties: { include_tags: { type: 'array' } } } },
      { name: 'gallery_recover_file_operations', label: '復原中斷檔案操作', description: '檢查並修復尚未完成的檔案操作日誌。', writes: true, enabled: true, background: true, example: {}, inputSchema: { type: 'object', properties: {} } },
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
          if (config.servers) config.servers = config.servers.map(server => server.id === 'gallery'
            ? { ...server, enabled: config.mcp_enabled, read_only: config.mcp_read_only }
            : server);
        }
        data = config;
      } else if (endpoint === '/api/settings/mcp/servers' && request.method() === 'POST') {
        const value = request.postDataJSON(); mutations.push(value);
        data = { ok: true, server: { ...value, server_name: value.id, builtin: false }, message: `已儲存 MCP 服務「${value.name}」。` };
      } else if (endpoint.startsWith('/api/settings/mcp/servers/') && request.method() === 'PUT') {
        const value = request.postDataJSON(); mutations.push(value);
        if (config.servers) {
          config.servers = config.servers.map(server => server.id === value.id ? { ...server, ...value } : server);
          data = { server: config.servers.find(server => server.id === value.id), config };
        } else data = { server: { ...value, server_name: value.id, builtin: false } };
      } else if (endpoint.startsWith('/api/settings/mcp/servers/') && request.method() === 'DELETE') {
        const deletedId = decodeURIComponent(endpoint.split('/').pop());
        mutations.push({ deleted: deletedId });
        data = { ok: true, deleted: deletedId, message: `已刪除 MCP 服務「${deletedId}」。` };
      } else if (endpoint === '/api/settings/mcp/test') data = { ok: true, message: '本機握手成功' };
      else if (endpoint === '/api/settings/mcp/client_config') data = { mcpServers: { 'gallery': { command: 'python', args: ['mcp_server.py'], disabled: false, disabledTools: ['gallery_recover_file_operations'] } } };
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
    await page.waitForSelector('#mcp-modal-tool-list input', { state: 'attached' });
    await page.waitForSelector('.mcp-list-card[data-server-id="gallery"]');
    assert.equal(await page.locator('#settings-mcp').isVisible(), true);
    assert.equal(await page.locator('#mcp-modal').isHidden(), true, 'MCP modal starts closed on card list view');
    assert.equal(await page.locator('.mcp-list-card').count(), 2, 'Both built-in Antigravity MCP cards are displayed');
    assert.ok((await page.locator('.mcp-list-card[data-server-id="gallery"] .mcp-list-card-title').textContent()).includes('Antigravity 圖庫 MCP'));
    assert.ok((await page.locator('.mcp-list-card[data-server-id="gallerypub"] .mcp-list-card-title').textContent()).includes('Antigravity 背景查證 MCP'));
    assert.equal(await page.locator('.mcp-list-card-key, .mcp-list-card-connection, .mcp-list-card-category').count(), 0, 'Cards omit identifiers, launch settings and categories');

    // Clicking the Antigravity gallery card opens the pop-up configuration modal.
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    assert.equal(await page.locator('#mcp-server-id, #mcp-server-category, #mcp-modal-picker').count(), 0, 'Internal identifiers and redundant metadata are removed from the form');
    assert.equal(await page.locator('#mcp-form input:visible').count(), 2, 'Built-in defaults expose only the two access switches');
    assert.equal(await page.locator('#mcp-server-command').isHidden(), true, 'Built-in launch settings start collapsed');
    await page.locator('#mcp-readonly').check();
    await page.locator('#mcp-save').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    assert.deepEqual(mutations[0], { mcp_enabled: true, mcp_read_only: true });
    assert.equal(await page.locator('.mcp-list-card[data-server-id="gallery"] .mcp-list-card-meta').textContent(), '1 / 4 個工具可用');
    assert.equal(await page.locator('[data-tool-name="gallery_batch_tag_or_move"]').isDisabled(), true);
    assert.equal(await page.locator('[data-tool-name="gallery_get_overview"]').isEnabled(), true);
    await page.locator('#mcp-test').click(); await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('握手成功'));
    await page.locator('#mcp-modal-client-details > summary').click();
    await page.locator('#mcp-export').click(); await page.waitForSelector('#mcp-client-config:visible');
    assert.equal(JSON.parse(await page.locator('#mcp-client-config').inputValue()).mcpServers['gallery'].disabled, false);
    assert.ok((await page.locator('#mcp-export-note').textContent()).includes('mcp_config.json'));
    assert.equal(await page.locator('#mcp-client-target').count(), 0);
    assert.equal(await page.locator('#settings-tab-gemini').count(), 0);
    assert.equal(await page.locator('#gemini-key').count(), 0);
    await page.locator('#mcp-modal-close').click();
    assert.equal(await page.locator('#mcp-modal').isHidden(), true);
    assert.equal(await page.evaluate(() => document.activeElement.matches('.mcp-card-edit[data-server-id="gallery"]')), true, 'Closing settings restores keyboard focus to its card');

    // Adding and deleting a custom MCP card from the UI.
    await page.locator('#mcp-card-new').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'mcp-server-name', 'New services start at the name, with no manual ID required');
    await page.locator('#mcp-save').click();
    assert.equal(mutations.length, 1, 'Missing names do not submit');
    await page.locator('#mcp-server-name').fill('ComfyUI 外部工作流 MCP');
    assert.equal(await page.locator('#mcp-server-command').isVisible(), true, 'New local services expose their required launch fields');
    assert.equal(await page.locator('#mcp-server-env').isHidden(), true, 'Optional local settings start collapsed');
    await page.locator('#mcp-save').click();
    assert.equal(mutations.length, 1, 'A local service requires a launch command');
    await page.locator('#mcp-server-transport').selectOption('http');
    assert.equal(await page.locator('#mcp-server-command').isHidden(), true, 'Remote services hide local launch fields');
    await page.locator('#mcp-server-url').fill('invalid-url');
    await page.locator('#mcp-save').click();
    assert.equal(mutations.length, 1, 'Invalid remote URLs do not submit');
    const remoteUrl = `http://127.0.0.1:8188/mcp/${'workflow-'.repeat(30)}`;
    await page.locator('#mcp-server-url').fill(remoteUrl);
    await page.locator('.mcp-dialog').screenshot({ path: path.join(directory, 'mcp-modal-custom.png'), animations: 'disabled' });
    await page.locator('#mcp-modal-advanced-details > summary').click();
    await page.locator('#mcp-server-custom-config').fill('workflow=inpaint\ntimeout_ms=15000');
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelectorAll('.mcp-list-card').length === 3);
    assert.match(mutations[1].id, /^mcp-[a-f0-9]{16}$/, 'The system assigns a valid service ID');
    assert.equal(mutations[1].transport, 'http');
    assert.equal(mutations[1].url, remoteUrl);
    assert.deepEqual(mutations[1].custom_config, { workflow: 'inpaint', timeout_ms: '15000' });
    const customCard = page.locator(`.mcp-list-card[data-server-id="${mutations[1].id}"]`);
    assert.equal((await customCard.textContent()).includes(remoteUrl), false, 'Remote connection details stay off the card');
    assert.equal(await page.locator('#mcp-server-url').inputValue(), remoteUrl, 'Connection details remain available in settings');
    assert.equal(await customCard.locator('.mcp-list-card-meta').textContent(), '工具數量待確認');
    await page.locator('#mcp-server-name').fill('ComfyUI 工作流');
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-draft-state').textContent === '已儲存');
    assert.equal(mutations.at(-1).id, mutations[1].id, 'Renaming a service retains its generated ID');
    assert.equal(mutations.at(-1).url, remoteUrl);
    assert.deepEqual(mutations.at(-1).custom_config, mutations[1].custom_config, 'Collapsed advanced values survive a name-only edit');
    await page.locator('#mcp-modal-advanced-details > summary').click();
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
    // Keep real connection data in settings while the cards show only the service summary.
    const command = 'C:\\Users\\Gary\\AppData\\Local\\Programs\\Python\\Python310\\python.exe';
    const args = ['H:\\code\\twitter-tag-gallery\\mcp_server.py'];
    config.servers = [
      { id: 'gallery', server_name: 'gallery', name: 'Antigravity 圖庫 MCP', category: '核心圖庫工具 · Antigravity', description: '由 Antigravity 呼叫圖庫工具（搜尋、掃描、標籤校正、歸檔與復原）。登入與模型選擇在客戶端管理。', builtin: true, transport: 'stdio', command, args, enabled: true, read_only: false },
      { id: 'gallerypub', server_name: 'gallerypub_ac9f611c', name: 'Antigravity 背景查證 MCP', category: '桌面背景 Sidecar · Gemini 3.8 Flash', description: '搭配桌面版 gallery-publication Sidecar，在背景啟動專用對話，自動執行原始發布日期追查與畫師跨平台身分查證。', builtin: true, transport: 'stdio', command, args, cwd: 'H:\\code\\twitter-tag-gallery', env: { TWITTER_GALLERY_ROOT: 'D:\\圖檔\\twitter' }, custom_config: { client: 'antigravity', nested: { preserve: true } }, tools: [toolCatalog[0]], tool_count: 1, enabled_tool_count: 1, enabled: true, read_only: false },
    ];
    await page.reload();
    await page.evaluate(() => openSettings('mcp'));
    await page.waitForSelector('.mcp-list-card[data-server-id="gallerypub"]');
    const cardText = await page.locator('#mcp-cards').textContent();
    for (const hiddenValue of ['Server Key', command, args[0], 'gallerypub_ac9f611c', '啟動指令', '固定模型：', '連線客戶端：', 'STDIO']) {
      assert.equal(cardText.includes(hiddenValue), false, `Cards omit ${hiddenValue}`);
    }
    assert.equal(await page.locator('.mcp-list-card[data-server-id="gallerypub"] .mcp-list-card-meta').textContent(), '1 / 1 個工具可用', 'Each service uses its own tool count');
    const sidecar = structuredClone(config.servers[1]);
    await page.locator('.mcp-list-card[data-server-id="gallerypub"] .mcp-card-edit').click();
    await page.locator('#mcp-readonly').check();
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-draft-state').textContent === '已儲存');
    for (const field of ['id', 'name', 'category', 'description', 'command', 'args', 'cwd', 'env', 'custom_config']) {
      assert.deepEqual(mutations.at(-1)[field], sidecar[field], `Simplified settings preserve existing ${field}`);
    }
    await page.locator('#mcp-readonly').uncheck();
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-draft-state').textContent === '已儲存');
    await page.locator('#mcp-modal-close').click();
    for (const width of [2560, 1440, 1064, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 900 }); await page.evaluate(() => switchSettingsTab('mcp'));
      await page.waitForSelector('#mcp-modal-tool-list input', { state: 'attached' });
      const overflow = await page.evaluate(() => {
        const content = document.querySelector('#ai-settings-content');
        return document.documentElement.scrollWidth > innerWidth + 1 || content.scrollWidth > content.clientWidth + 1;
      });
      assert.equal(overflow, false, `No page or workspace overflow at ${width}px`);
      const escaped = await page.locator('.mcp-list-card').evaluateAll(cards => cards.flatMap(card => {
        const bounds = card.getBoundingClientRect();
        return [...card.querySelectorAll('*')].filter(node => {
          const rect = node.getBoundingClientRect();
          return rect.width && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1);
        }).map(node => node.className.baseVal ?? node.className);
      }));
      assert.deepEqual(escaped, [], `Card contents stay inside their own card at ${width}px`);
      if (width >= 1440) {
        const cards = await page.locator('.mcp-list-card').all();
        const first = await cards[0].boundingBox(), second = await cards[1].boundingBox();
        assert.equal(first.y, second.y, 'Desktop displays the two services side by side');
        assert.equal(first.height, second.height, 'Desktop card actions align');
      }
      await page.locator('.mcp-list-card[data-server-id="gallerypub"] .mcp-card-edit').click();
      const dialogFits = await page.locator('.mcp-dialog').evaluate(dialog => {
        const rect = dialog.getBoundingClientRect(), body = document.querySelector('#mcp-modal-body');
        return rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight + 1 && body.scrollWidth <= body.clientWidth + 1;
      });
      assert.equal(dialogFits, true, `Compact modal fits ${width}px`);
      if ([1440, 390].includes(width)) await page.locator('.mcp-dialog').screenshot({ path: path.join(directory, `mcp-modal-${width}.png`), animations: 'disabled' });
      await page.locator('#mcp-modal-advanced-details > summary').click();
      assert.equal(await page.locator('#mcp-server-command').isVisible(), true, 'Advanced launch settings remain available');
      assert.equal(await page.locator('#mcp-server-env').isVisible(), true, 'Environment settings remain available');
      assert.equal(await page.locator('#mcp-modal-body').evaluate(body => body.scrollWidth <= body.clientWidth + 1), true, `Advanced modal fits ${width}px`);
      if (width === 1440) await page.screenshot({ path: path.join(directory, 'mcp-modal-advanced.png'), animations: 'disabled' });
      await page.locator('#mcp-modal-close').click();
    }
    const toggle = page.getByRole('switch', { name: '啟用 Antigravity 圖庫 MCP', exact: true });
    await toggle.focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.querySelector('#mcp-page-feedback').textContent.includes('已停用'));
    assert.equal(await page.locator('#mcp-modal').isHidden(), true, 'Toggling a card does not open its settings');
    assert.equal(mutations.at(-1).mcp_enabled, false);
    assert.equal(await page.locator('.mcp-list-card[data-server-id="gallery"] .mcp-list-card-meta').textContent(), '0 / 4 個工具可用');
    await toggle.press('Space');
    await page.waitForFunction(() => document.querySelector('#mcp-page-feedback').textContent.includes('已啟用'));
    await page.getByRole('button', { name: '測試連線：Antigravity 圖庫 MCP', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#mcp-page-feedback').textContent.includes('握手成功'));
    assert.equal(await page.locator('#mcp-modal').isHidden(), true, 'Testing a card does not open its settings');
    await page.evaluate(() => {
      document.querySelector('#mcp-page-feedback').textContent = '';
      document.querySelector('#ai-settings-content').scrollTop = 0;
    });
    await page.screenshot({ path: path.join(directory, 'settings-mcp-mobile.png'), fullPage: true });
    await page.evaluate(() => switchSettingsTab('tools'));
    await page.locator('#agent-tool-catalog').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => switchSettingsTab('mcp'));
    await page.screenshot({ path: path.join(directory, 'settings-mcp-desktop.png'), fullPage: true });
    await page.locator('#mcp-cards').screenshot({ path: path.join(directory, 'settings-mcp-cards.png'), animations: 'disabled' });
    await page.evaluate(() => switchSettingsTab('tools'));
    await page.locator('#agent-tool-catalog').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(directory, 'settings-tools-desktop.png'), fullPage: true });
    await page.evaluate(() => switchSettingsTab('artists'));
    await page.waitForFunction(() => document.querySelector('#artist-model-status').textContent.includes('5'));
    assert.ok((await page.locator('#artist-model-status').textContent()).includes('MCP 客戶端'));
    assert.equal(await page.locator('#artist-model-enabled').count(), 0);
    assert.equal(await page.locator('#artist-model-executable').count(), 0);
    assert.ok((await page.locator('#artist-mcp-prompt').inputValue()).includes('待補查畫師身分'));
    await page.screenshot({ path: path.join(directory, 'artist-identity-settings-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.screenshot({ path: path.join(directory, 'artist-identity-settings-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []); assert.equal(calls.filter(value => ['/api/ai/status', '/api/reverse_search/status'].includes(value)).length, 0);
    console.log(JSON.stringify({ result: 'PASS', mutations: mutations.length, browserErrors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
