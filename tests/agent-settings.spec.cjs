/* Browser acceptance: real UI assets, mocked jobs and persistent prompt revisions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1320, height: 1000 } });
    const errors = [], writes = [], permissionWrites = [];
    page.on('pageerror', error => errors.push(error.stack));
    let revision = 0, description = '原工具說明';
    let permissions = { mcp_enabled: true, mcp_read_only: false, disabled_tools: ['gallery_get_overview'] }, failPermissionSave = false, pendingPermissionSave = null;
    const tool = () => ({ name: 'gallery_trace_publication_dates', label: '查詢原始發布日期', description, usage_prompt: description, default_usage_prompt: '原工具說明', writes: true, enabled: permissions.mcp_enabled && !permissions.mcp_read_only && !permissions.disabled_tools.includes('gallery_trace_publication_dates'), prompt_revision: revision, inputSchema: { type: 'object' }, example: { job_id: 'a'.repeat(32), image_ids: [1] } });
    const catalog = () => [tool(), { name: 'gallery_get_overview', label: '圖庫概覽', description: '讀取圖庫統計', writes: false, enabled: permissions.mcp_enabled && !permissions.disabled_tools.includes('gallery_get_overview') }];
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8') });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      if (endpoint === '/api/settings/mcp/tools') return route.fulfill({ json: { tools: catalog() } });
      if (endpoint.endsWith('/prompt') && request.method() === 'PUT') {
        const value = request.postDataJSON(); writes.push(value); description = value.usage_prompt; revision += 1;
        return route.fulfill({ json: tool() });
      }
      if (endpoint === '/api/agent/jobs' && request.method() === 'POST') { writes.push(request.postDataJSON()); return route.fulfill({ status: 202, json: { id: 'b'.repeat(32), status: 'queued' } }); }
      if (endpoint === '/api/agent/jobs') return route.fulfill({ json: { jobs: [], bridge: { connected: false } } });
      if (endpoint === '/api/rename/publication-preview') return route.fulfill({ json: { total: 2, verified: 0, pending: 2, items: [{ id: 1, folder: 'artist', filename: 'original.png', published_at: null }] } });
      if (endpoint === '/api/settings/integrations') {
        if (request.method() === 'POST') {
          const value = request.postDataJSON(); permissionWrites.push(value);
          if (pendingPermissionSave) await pendingPermissionSave;
          if (failPermissionSave) return route.fulfill({ status: 500, json: { detail: '暫時無法儲存工具權限' } });
          permissions = { ...permissions, ...value };
        }
        return route.fulfill({ json: { ...permissions, tools: catalog(), sessions: [] } });
      }
      if (endpoint === '/api/runtime') return route.fulfill({ json: { active_job: {}, jobs: [], ai: { running: false }, reverse: {}, schedule: { config: { schedule_times: [], source_dirs: [] } }, model: {} } });
      if (endpoint === '/api/folders') return route.fulfill({ json: [{ name: 'artist', count: 2 }] });
      if (endpoint === '/api/images') return route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 50, pages: 1 } });
      if (endpoint === '/api/stats') return route.fulfill({ json: { total_images: 2, total_folders: 1, total_tags: 0, root_unorganized: 0, folder_organized: 2, ai_tagged: 0, suggested_folder_count: 0, ai_status: { running: false } } });
      if (endpoint === '/api/tags') return route.fulfill({ json: { items: [], has_more: false, next_offset: 0 } });
      if (endpoint === '/api/schedule/status') return route.fulfill({ json: { running_job: false, config: { schedule_times: [], source_dirs: [], history_logs: [] } } });
      return route.fulfill({ json: {} });
    });
    await page.goto('http://gallery.test/');
    await page.evaluate(() => openSettings('tools'));
    await page.waitForSelector('.agent-tool-row');
    assert.equal(await page.locator('#mcp-tool-list, #tools-permission-save').count(), 0);
    assert.equal(await page.locator('#mcp-tool-workbench').count(), 0);
    assert.equal(await page.locator('#mcp-tool-arguments').count(), 0);
    assert.equal(await page.locator('#mcp-tool-history').count(), 0);
    assert.equal(await page.locator('#agent-prompt-schema').count(), 0);
    assert.equal(await page.locator('#agent-prompt-example').count(), 0);
    assert.equal(await page.locator('.agent-tool-row-content').first().textContent(), '原工具說明');
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);
    await page.locator('.agent-tool-row').first().click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), true);
    await page.locator('#agent-usage-prompt').fill('編輯後的工具說明');
    assert.equal(await page.locator('#agent-task-prompt').count(), 0);
    assert.equal(await page.locator('#agent-tool-modal-extra').count(), 0);
    await page.locator('#agent-prompt-save').click();
    await page.waitForFunction(() => document.getElementById('agent-prompt-feedback').textContent.includes('已儲存'));
    assert.equal(writes[0].revision, 0);
    assert.equal(Object.hasOwn(writes[0], 'task_prompt'), false);
    assert.equal(await page.locator('.agent-tool-row-content').first().textContent(), '編輯後的工具說明');
    await page.locator('#agent-prompt-close').click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);

    // A switch persists only its own permission, without opening the prompt editor.
    const publicationSwitch = page.getByRole('switch', { name: '啟用 查詢原始發布日期', exact: true });
    const overviewSwitch = page.getByRole('switch', { name: '啟用 圖庫概覽', exact: true });
    assert.equal(await publicationSwitch.isChecked(), true);
    assert.equal(await overviewSwitch.isChecked(), false);
    let releasePermissionSave;
    pendingPermissionSave = new Promise(resolve => { releasePermissionSave = resolve; });
    await publicationSwitch.uncheck();
    await page.waitForFunction(() => [...document.querySelectorAll('.agent-tool-toggle')].every(input => input.disabled));
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);
    releasePermissionSave();
    pendingPermissionSave = null;
    await page.waitForFunction(() => document.querySelector('#agent-tool-feedback').textContent.includes('已停用並儲存'));
    assert.deepEqual(permissionWrites[0], { disabled_tools: ['gallery_get_overview', 'gallery_trace_publication_dates'] });
    assert.equal(await page.locator('[data-tool-name="gallery_trace_publication_dates"]').isChecked(), false);
    await page.reload();
    await page.evaluate(() => openSettings('tools'));
    await page.waitForSelector('.agent-tool-toggle');
    assert.equal(await publicationSwitch.isChecked(), false, 'Disabled state survives reloading');

    // Keyboard activation and failed saves keep the displayed state consistent with saved permissions.
    await overviewSwitch.focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.querySelector('#agent-tool-feedback').textContent.includes('已啟用並儲存'));
    assert.deepEqual(permissions.disabled_tools, ['gallery_trace_publication_dates']);
    assert.equal(await overviewSwitch.evaluate(input => input === document.activeElement), true);
    failPermissionSave = true;
    await publicationSwitch.check();
    await page.waitForFunction(() => document.querySelector('#agent-tool-feedback').textContent.includes('暫時無法儲存'));
    assert.equal(await publicationSwitch.isChecked(), false, 'Failed saves restore the previous switch state');
    assert.deepEqual(permissions.disabled_tools, ['gallery_trace_publication_dates']);
    failPermissionSave = false;

    const directory = path.join(root, '.tag_gallery/ui-review'); fs.mkdirSync(directory, { recursive: true });
    await page.evaluate(() => { document.querySelector('#agent-tool-feedback').textContent = ''; });
    await page.screenshot({ path: path.join(directory, 'tool-switch-desktop.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    if (await page.evaluate(() => document.body.classList.contains('shell-drawer-open'))) await page.locator('#shell-close-filters').click();
    assert.equal(await page.locator('.agent-tool-row').evaluateAll(rows => rows.every(row => row.scrollWidth <= row.clientWidth + 1)), true);
    await page.screenshot({ path: path.join(directory, 'tool-switch-mobile.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 1320, height: 1000 });

    // An unrelated MCP draft must not overwrite the newly saved tool permission.
    await page.evaluate(() => { openSettings('mcp'); openMcpModal('gallery'); });
    await page.locator('#mcp-readonly').check();
    await page.locator('#mcp-modal-close').click();
    await page.evaluate(() => openSettings('tools'));
    await publicationSwitch.check();
    await page.waitForFunction(() => document.querySelector('#agent-tool-feedback').textContent.includes('已啟用並儲存'));
    await page.evaluate(() => { openSettings('mcp'); openMcpModal('gallery'); });
    assert.equal(await page.locator('#mcp-readonly').isChecked(), true, 'Switching a tool preserves the unrelated MCP draft');
    assert.equal(await page.locator('[data-tool-name="gallery_trace_publication_dates"]').isChecked(), true);
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    assert.deepEqual(permissionWrites.at(-1), { mcp_enabled: true, mcp_read_only: true });
    assert.deepEqual(permissions.disabled_tools, []);
    await page.locator('#mcp-modal-close').click();

    // Saved MCP policy takes precedence over per-tool switches.
    permissions.mcp_read_only = true;
    await page.reload();
    await page.evaluate(() => openSettings('tools'));
    await page.waitForSelector('.agent-tool-toggle');
    assert.equal(await publicationSwitch.isDisabled(), true);
    assert.equal(await overviewSwitch.isEnabled(), true);
    assert.ok((await publicationSwitch.locator('..').getAttribute('title')).includes('唯讀模式'));
    permissions.mcp_enabled = false;
    await page.reload();
    await page.evaluate(() => openSettings('tools'));
    await page.waitForSelector('.agent-tool-toggle');
    assert.equal(await publicationSwitch.isDisabled(), true);
    assert.equal(await overviewSwitch.isDisabled(), true);
    permissions.mcp_enabled = true;
    permissions.mcp_read_only = false;

    await page.evaluate(() => { closeScheduleModal(); openRenameModal(); });
    await page.waitForFunction(() => document.getElementById('rn-summary-bar').textContent.includes('待追查'));
    assert.equal(await page.locator('#rn-date').isVisible(), false);
    assert.ok((await page.locator('#rn-preview-box').textContent()).includes('未確認前保留原名'));
    await page.locator('#btn-rn-exec').click();
    await page.waitForFunction(() => document.getElementById('rn-summary-bar').textContent.includes('工作已排入'));
    assert.equal(writes[1].workflow, 'publication');
    assert.equal(writes[1].pad_digits, 4);
    await page.evaluate(() => closeRenameModal());
    assert.equal(writes.length, 2); // Closing the modal does not cancel background work.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => openSettings('tools'));
    assert.ok(await page.locator('#agent-tool-search').isVisible());
    assert.deepEqual(errors, []);
    console.log('Agent settings browser acceptance passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
