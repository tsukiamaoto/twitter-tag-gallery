/* Isolated workspace regression: every API and thumbnail is mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery/ui-review');
const templates = JSON.parse(fs.readFileSync(path.join(root, 'image_skill_templates.json'), 'utf8'));

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], calls = [], writes = [];
    let configuration = { mcp_enabled: true, mcp_read_only: false, disabled_tools: [], sessions: [] };
    const catalog = [
      { name: 'gallery_get_overview', label: '圖庫概覽', description: '讀取圖庫統計', writes: false, example: {}, inputSchema: { type: 'object' } },
      { name: 'gallery_recover_file_operations', label: '復原中斷檔案操作', description: '修復中斷的檔案操作', writes: true, background: true, example: {}, inputSchema: { type: 'object' } },
    ];
    const ai = { running: false, model_loaded: false, status_message: '待機中' };
    const schedule = { running_job: false, config: { enabled: false, schedule_times: [], source_dirs: [], history_logs: [] } };
    const gguf = { models: [{ id: templates[0].model_id, filename: 'WAI-illustrious.gguf', profile: 'wai_illustrious', valid: true, bytes: 2e9 }], directory: 'mock/models/gguf', selection: { model_id: templates[0].model_id, profile: 'wai_illustrious' }, runtime: { phase: 'unloaded' }, download: {}, missing: [] };
    const tags = Array.from({ length: 55 }, (_, i) => ({ name: `study_${i}`, zh_name: `風景分類 ${i}`, category: 'general', count: 120 }));
    const pictures = Array.from({ length: 120 }, (_, i) => ({ id: i + 1, filename: `study_${i}.jpg`, rel_path: `study/study_${i}.jpg`, folder: 'study', mtime: 1, media_type: 'image', artists: [], characters: [], copyrights: [], preview_tags: [], rating: 'safe', ai_tagged: false }));
    let releaseSkills;
    const firstSkillRead = new Promise(resolve => { releaseSkills = resolve; });
    let delaySkills = true, run = null;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      calls.push(endpoint);
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8') });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      if (/^\/api\/(thumb|raw)\//.test(endpoint)) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="500"><rect width="400" height="500" fill="#334b60"/><circle cx="260" cy="130" r="55" fill="#aec3b7"/><path d="M0 400 130 180 300 430 400 290v210H0Z" fill="#708780"/></svg>' });
      let data = {};
      if (endpoint === '/api/settings/integrations') {
        if (request.method() === 'POST') { const body = request.postDataJSON(); writes.push({ endpoint, body }); configuration = { ...configuration, ...body }; }
        data = { ...configuration, tools: catalog };
      } else if (endpoint === '/api/settings/mcp/tools') data = { tools: catalog.map(tool => ({ ...tool, enabled: configuration.mcp_enabled && !configuration.disabled_tools.includes(tool.name) && !(configuration.mcp_read_only && tool.writes) })) };
      else if (endpoint.endsWith('/run')) {
        writes.push({ endpoint, body: request.postDataJSON() });
        run = { id: 'a'.repeat(32), name: 'gallery_get_overview', status: 'running', started_at: 1792454400 };
        data = run;
      } else if (endpoint === '/api/settings/mcp/runs') data = { runs: run ? [run] : [] };
      else if (endpoint.startsWith('/api/settings/mcp/runs/')) data = { run };
      else if (endpoint === '/api/settings/artist-identity') data = { identities: 5, pending_count: 2, requests: [] };
      else if (endpoint === '/api/agent/jobs') data = { jobs: [], bridge: { connected: false } };
      else if (endpoint === '/api/image-skills') {
        if (delaySkills) { delaySkills = false; await firstSkillRead; }
        data = { skills: structuredClone(templates), active_id: templates[0].id };
      } else if (endpoint === '/api/models/gguf') data = gguf;
      else if (endpoint === '/api/images') {
        const size = Number(url.searchParams.get('page_size') || 60), page = Number(url.searchParams.get('page') || 1);
        data = { items: pictures.slice((page - 1) * size, page * size), total: 120, page, page_size: size };
      } else if (endpoint === '/api/tags') data = { items: tags, next_offset: tags.length, has_more: false };
      else if (endpoint === '/api/folders') data = [{ name: 'study', count: 120 }];
      else if (endpoint === '/api/stats') data = { total_images: 120, root_unorganized: 0, folder_organized: 120, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/runtime') data = { ai, reverse: { running: false }, schedule, active_job: {}, jobs: [], model: {} };
      else if (endpoint === '/api/schedule/status') data = schedule;
      else if (request.method() !== 'GET') throw new Error(`Unexpected write: ${endpoint}`);
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://workspace.test/');
    await page.waitForFunction(() => document.querySelectorAll('.card').length === 60 && document.querySelector('#shell-load-status').hidden);
    assert.equal(await page.locator('#workspace-home').getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('#workspace-rail').evaluate(el => el.getBoundingClientRect().width), 56);
    assert.equal(calls.includes('/api/models/gguf'), false, 'Home does not load AI settings');
    const searched = page.waitForResponse(response => new URL(response.url()).pathname === '/api/images' && new URL(response.url()).searchParams.get('q') === 'study');
    await page.locator('#main-search').fill('study');
    await searched;
    await page.waitForFunction(() => document.querySelector('#shell-load-status').hidden);
    await page.locator('#sel-sort').selectOption('size_desc');
    await page.waitForFunction(() => document.querySelector('#shell-load-status').hidden);
    await page.evaluate(() => changePage(1));
    await page.waitForFunction(() => state.page === 2 && document.querySelector('#shell-load-status').hidden);
    await page.locator('.card-check').first().click();
    await page.evaluate(() => {
      document.querySelector('#gallery-scroll').scrollTop = 350;
      document.querySelector('#sidebar-tag-list').scrollTop = 250;
      window.savedGallery = document.querySelector('#image-grid');
    });
    const homeState = () => page.evaluate(() => ({ page: state.page, selected: [...state.selectedIds], tags: [...state.includeTags], search: document.querySelector('#main-search').value, sort: document.querySelector('#sel-sort').value, scroll: document.querySelector('#gallery-scroll').scrollTop, tagScroll: document.querySelector('#sidebar-tag-list').scrollTop, sidebar: document.querySelector('.sidebar-tab[aria-selected="true"]').id }));
    const before = await homeState(), imageRequests = calls.filter(value => value === '/api/images').length;
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, 'workspace-home-desktop.png'), animations: 'disabled' });
    await page.locator('#workspace-ai').click();
    assert.equal(await page.locator('#settings-model').isVisible(), true);
    assert.equal(await page.locator('#schedule-modal').isVisible(), false);
    assert.equal(await page.locator('#ai-category-list [role="tab"]').count(), 4);
    await page.locator('#ai-page-title').focus();
    for (const key of ['/', 'ArrowRight', 'Delete']) await page.keyboard.press(key);
    assert.equal(calls.filter(value => value === '/api/images').length, imageRequests);
    assert.deepEqual(writes, []);
    await page.locator('#btn-settings').click();
    assert.deepEqual(await page.locator('.settings-tabs [role="tab"]').allTextContents(), ['瀏覽偏好', '操作日誌與紀錄', '背景任務與復原']);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#schedule-modal').isVisible(), false);

    await page.locator('#settings-tab-model').focus(); await page.keyboard.press('ArrowDown');
    await page.waitForSelector('.mcp-list-card[data-server-id="gallery"]');
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    await page.waitForFunction(() => !document.querySelector('#mcp-readonly').disabled);
    await page.locator('#mcp-readonly').check();
    await page.locator('#mcp-modal-close').click();
    assert.equal(await page.locator('#mcp-modal').isHidden(), true);
    assert.ok((await page.locator('.mcp-list-card[data-server-id="gallery"]').textContent()).includes('尚未儲存'));
    await page.locator('#settings-tab-tools').click();
    assert.equal(await page.locator('#settings-tools').getByText('工具權限', { exact: true }).count(), 0);
    await page.waitForSelector('.agent-tool-toggle');
    await page.waitForSelector('#agent-tool-catalog button');
    assert.equal(await page.locator('.agent-tool-row-content').first().textContent(), '讀取圖庫統計');
    await page.locator('#agent-tool-catalog button').first().click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), true);
    await page.locator('#agent-usage-prompt').fill('Keep this tool prompt draft.');
    await page.locator('#agent-prompt-close').click();
    assert.equal(await page.locator('#agent-tool-modal').isVisible(), false);
    const recoverySwitch = page.getByRole('switch', { name: '啟用 復原中斷檔案操作', exact: true });
    assert.equal(await recoverySwitch.isEnabled(), true, 'Unsaved MCP changes do not affect tool permissions');
    await recoverySwitch.uncheck();
    await page.waitForFunction(() => document.querySelector('#agent-tool-feedback').textContent.includes('已停用並儲存'));
    assert.deepEqual(writes[0].body, { disabled_tools: ['gallery_recover_file_operations'] });
    await page.locator('#settings-tab-mcp').click();
    await page.locator('.mcp-list-card[data-server-id="gallery"]').click();
    assert.equal(await page.locator('#mcp-modal').isVisible(), true);
    assert.equal(await page.locator('#mcp-readonly').isChecked(), true, 'Other saves preserve the MCP draft');
    await page.locator('#mcp-save').click();
    await page.waitForFunction(() => document.querySelector('#mcp-feedback').textContent.includes('立即套用'));
    await page.locator('#mcp-modal-close').click();
    assert.equal(await page.locator('#mcp-modal').isHidden(), true);
    assert.deepEqual(writes[1].body, { mcp_enabled: true, mcp_read_only: true });
    assert.deepEqual(configuration.disabled_tools, ['gallery_recover_file_operations']);
    await page.locator('#settings-tab-tools').click();
    assert.equal(await page.locator('#agent-usage-prompt').inputValue(), 'Keep this tool prompt draft.');

    await page.evaluate(() => openSettings('artists'));
    await page.waitForFunction(() => document.querySelector('#artist-model-status').textContent.includes('5'));
    assert.equal(await page.locator('#settings-tab-tools').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#settings-tab-artists').getAttribute('aria-selected'), 'true');
    const artistPrompt = await page.locator('#artist-mcp-prompt').inputValue();
    await page.locator('#settings-tab-image-skills').click();
    await page.locator('#image-skill-new').click();
    await page.locator('#image-skill-prompt').fill('Draft typed while settings are loading.');
    releaseSkills();
    await page.waitForFunction(() => document.querySelector('#image-skills-active').textContent.includes('目前套用'));
    assert.equal(await page.locator('#image-skill-prompt').inputValue(), 'Draft typed while settings are loading.');
    await page.locator('#image-skills-models').click();
    await page.locator('#settings-tab-image-skills').click();
    assert.equal(await page.locator('#image-skill-prompt').inputValue(), 'Draft typed while settings are loading.');
    await page.locator('#image-skill-modal-close').click();
    await page.locator('#workspace-home').click();
    assert.deepEqual(await homeState(), before);
    assert.equal(await page.evaluate(() => window.savedGallery === document.querySelector('#image-grid')), true);
    assert.equal(calls.filter(value => value === '/api/images').length, imageRequests);
    await page.evaluate(() => { document.querySelector('#gallery-scroll').scrollTop = 180; });
    await page.locator('#workspace-home').click();
    assert.equal(await page.locator('#gallery-scroll').evaluate(el => el.scrollTop), 180, 'Clicking the active Home button preserves its current scroll');
    await page.locator('#workspace-ai').click();
    assert.equal(await page.locator('#settings-image-skills').isVisible(), true, 'AI category is remembered within this page session');
    await page.evaluate(() => openSettings('artists'));
    assert.equal(await page.locator('#artist-mcp-prompt').inputValue(), artistPrompt);

    await page.evaluate(() => openAiSettings('tool-catalog'));
    assert.equal(await page.locator('#agent-tool-catalog .agent-tool-row').count(), 2);
    const modelRead = page.waitForResponse(response => new URL(response.url()).pathname === '/api/models/gguf');
    await page.evaluate(() => openAiSettings('model'));
    await modelRead;
    await page.locator('#workspace-home').click();
    const modelReads = calls.filter(value => value === '/api/models/gguf').length;
    await page.waitForTimeout(2200);
    assert.equal(calls.filter(value => value === '/api/models/gguf').length, modelReads, 'Leaving AI models stops its polling');

    const widths = [1440, 1064, 768, 390, 320];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 1000 });
      for (const section of ['model', 'mcp', 'tools', 'image-skills']) {
        await page.evaluate(section => openAiSettings(section), section);
        const overflow = await page.evaluate(() => {
          const content = document.querySelector('#ai-settings-content');
          return document.documentElement.scrollWidth > innerWidth + 1 || content.scrollWidth > content.clientWidth + 1;
        });
        assert.equal(overflow, false, `${section} fits ${width}px`);
        if (section === 'mcp') {
          const card = await page.locator('.mcp-list-card').first().boundingBox();
          assert.ok(card.x >= 0 && card.x + card.width <= width && card.y + card.height <= 1000, `MCP card remains visible at ${width}px`);
        }
        if (section === 'image-skills') {
          await page.locator('.image-skill-list-card').first().waitFor({ state: 'visible' });
          const card = await page.locator('.image-skill-list-card').first().boundingBox();
          assert.ok(card.x >= 0 && card.x + card.width <= width && card.y + card.height <= 1000, `Skill card remains visible at ${width}px`);
        }
        if ([1440, 390].includes(width)) await page.screenshot({ path: path.join(artifacts, `workspace-${section}-${width}.png`), animations: 'disabled' });
      }
      if (width <= 800) {
        assert.equal(await page.locator('#workspace-rail').evaluate(el => el.getBoundingClientRect().width), 48);
        await page.locator('#ai-open-navigation').click();
        await page.waitForFunction(() => Math.abs(document.querySelector('.shell-sidebar').getBoundingClientRect().left - 48) < 1);
        assert.equal(await page.locator('main').evaluate(el => el.inert), true);
        await page.locator('#settings-tab-mcp').click();
        assert.equal(await page.locator('main').evaluate(el => el.inert), false);
        assert.equal(await page.locator('#ai-open-navigation').getAttribute('aria-expanded'), 'false');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'ai-page-title');
        await page.locator('#ai-open-navigation').click();
        await page.keyboard.press('Escape');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'ai-open-navigation');
      }
    }
    await page.reload();
    await page.waitForSelector('.card');
    assert.equal(await page.locator('#workspace-home').getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('#ai-workspace').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', widths, mockedWrites: writes.length, browserErrors: errors.length, screenshots: artifacts }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
