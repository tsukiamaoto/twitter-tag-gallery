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
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    let revision = 0, description = '原工具說明', task = '原任務 Prompt';
    const tool = () => ({ name: 'gallery_trace_publication_dates', label: '查詢原始發布日期', description, usage_prompt: description, default_usage_prompt: '原工具說明', writes: true, enabled: true, workflow: 'publication', prompt_revision: revision, task_prompt: { prompt: task, revision }, inputSchema: { type: 'object' }, example: { job_id: 'a'.repeat(32), image_ids: [1] } });
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8') });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      if (endpoint === '/api/settings/mcp/tools') return route.fulfill({ json: { tools: [tool()] } });
      if (endpoint.endsWith('/prompt') && request.method() === 'PUT') {
        const value = request.postDataJSON(); writes.push(value); description = value.usage_prompt; task = value.task_prompt; revision += 1;
        return route.fulfill({ json: tool() });
      }
      if (endpoint === '/api/agent/jobs' && request.method() === 'POST') { writes.push(request.postDataJSON()); return route.fulfill({ status: 202, json: { id: 'b'.repeat(32), status: 'queued' } }); }
      if (endpoint === '/api/agent/jobs') return route.fulfill({ json: { jobs: [], bridge: { connected: false } } });
      if (endpoint === '/api/rename/publication-preview') return route.fulfill({ json: { total: 2, verified: 0, pending: 2, items: [{ id: 1, folder: 'artist', filename: 'original.png', published_at: null }] } });
      if (endpoint === '/api/settings/integrations') return route.fulfill({ json: { mcp_enabled: true, mcp_read_only: false, disabled_tools: [], tools: [tool()], sessions: [] } });
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
    await page.locator('#agent-task-prompt').fill('編輯後的完整查證 Prompt');
    await page.locator('#agent-prompt-save').click();
    await page.waitForFunction(() => document.getElementById('agent-prompt-feedback').textContent.includes('已儲存'));
    assert.equal(writes[0].revision, 0);
    assert.equal(writes[0].task_prompt, '編輯後的完整查證 Prompt');
    assert.equal(await page.locator('.agent-tool-row-content').first().textContent(), '編輯後的工具說明');
    await page.locator('#agent-prompt-close').click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);
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
