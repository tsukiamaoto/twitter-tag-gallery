/* Real UI assets with isolated skill documents and fault-injected HTTP responses. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const hash = content => crypto.createHash('sha256').update(content).digest('hex');
(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: {
      writeText: async value => { window.copiedSkill = value; }
    } }));
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.stack));
    const ids = ['gallery-deduplication', 'twitter-tag-correlation', 'gallery-artist-identity', 'gallery-publication-tracing'];
    const documents = Object.fromEntries(ids.map(id => {
      const content = fs.readFileSync(path.join(root, '.agents/skills', id, 'SKILL.md'), 'utf8').replace(/\r?\n/g, '\r\n');
      return [id, { id, name: id, content, revision: hash(content), path: `mock/.agents/skills/${id}/SKILL.md`, description: `流程說明 ${id}`, valid: true, error: '' }];
    }));
    let failSave = false;
    await page.route('**/*', async route => {
      const request = route.request(), endpoint = new URL(request.url()).pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8') });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      if (endpoint === '/api/agent-skills') return route.fulfill({ json: { skills: Object.values(documents).map(({ content, ...item }) => item) } });
      if (endpoint.startsWith('/api/agent-skills/')) {
        const id = endpoint.split('/').at(-1);
        if (request.method() === 'PUT') {
          const value = request.postDataJSON(); writes.push(value);
          if (failSave) return route.fulfill({ status: 500, json: { detail: '模擬寫入失敗，原檔保留' } });
          if (value.revision !== documents[id].revision) return route.fulfill({ status: 409, json: { detail: '技能已被其他視窗更新，請重新載入' } });
          documents[id] = { ...documents[id], content: value.content, revision: hash(value.content) };
        }
        return route.fulfill({ json: documents[id] });
      }
      let data = {};
      if (endpoint === '/api/image-skills') data = { skills: [], active_id: null };
      if (endpoint === '/api/models/gguf') data = { models: [], selection: {}, runtime: {}, download: {}, missing: [] };
      if (endpoint === '/api/runtime') data = { ai: {}, reverse: {}, schedule: { config: {} }, jobs: [], active_job: {}, model: {} };
      if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: {} };
      if (endpoint === '/api/folders') data = [];
      if (endpoint === '/api/tags') data = { items: [], has_more: false };
      if (endpoint === '/api/images') data = { items: [], total: 0 };
      if (endpoint === '/api/settings/integrations') data = { mcp_enabled: true, disabled_tools: [], tools: [], sessions: [] };
      if (endpoint === '/api/settings/mcp/tools') data = { tools: [] };
      return route.fulfill({ json: data });
    });
    await page.goto('http://agent-skills.test/');
    await page.evaluate(() => openAiSettings('image-skills'));
    await page.waitForSelector('.agent-skill-card');
    assert.equal(await page.locator('.agent-skill-card').count(), 4);
    assert.equal(await page.locator('.agent-skill-card').getByText('檔案可讀', { exact: true }).count(), 4);
    assert.equal(await page.locator('.agent-skill-card').getByText('已啟用', { exact: true }).count(), 0);
    const id = ids[0], card = page.locator(`[data-agent-skill-id="${id}"] button`);
    await card.click();
    await page.waitForSelector('#agent-skill-modal:visible');
    const normalized = documents[id].content.replace(/\r\n/g, '\n');
    assert.equal(await page.locator('#agent-skill-content').inputValue(), normalized);
    assert.equal(await page.locator('#agent-skill-save').isDisabled(), true, 'CRLF documents start without a false dirty state');
    const edited = normalized + '\n新增流程規則。\n';
    await page.locator('#agent-skill-content').fill(edited);
    await page.locator('#agent-skill-copy').click();
    assert.equal(await page.evaluate(() => window.copiedSkill), edited);
    await page.locator('#agent-skill-close').click();
    await card.click();
    assert.equal(await page.locator('#agent-skill-content').inputValue(), edited, 'Closing and reopening retains the draft');
    await page.evaluate(() => openAiSettings('tools'));
    assert.equal(await page.locator('#agent-skill-modal').isHidden(), true);
    await page.evaluate(() => openAiSettings('image-skills'));
    assert.equal(await page.locator('#agent-skill-content').inputValue(), edited);
    await page.locator('#agent-skill-content').press('Control+s');
    await page.waitForFunction(() => document.querySelector('#agent-skill-feedback').textContent.includes('技能已儲存'));
    assert.equal(documents[id].content, edited);
    assert.deepEqual(Object.keys(writes[0]).sort(), ['content', 'revision']);
    assert.equal(await page.locator('#agent-skill-save').isDisabled(), true);
    const draft = edited + '\n保留失敗草稿。';
    await page.locator('#agent-skill-content').fill(draft);
    failSave = true;
    await page.locator('#agent-skill-save').click();
    await page.waitForFunction(() => document.querySelector('#agent-skill-feedback').textContent.includes('模擬寫入失敗'));
    assert.equal(await page.locator('#agent-skill-content').inputValue(), draft);
    failSave = false;
    documents[id] = { ...documents[id], content: edited + '\n外部修改', revision: hash(edited + '\n外部修改') };
    await page.locator('#agent-skill-save').click();
    await page.waitForFunction(() => document.querySelector('#agent-skill-feedback').textContent.includes('其他視窗更新'));
    assert.equal(await page.locator('#agent-skill-content').inputValue(), draft);
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#agent-skill-reload').click();
    await page.waitForFunction(content => document.querySelector('#agent-skill-content').value === content, documents[id].content);
    assert.equal(await page.locator('#agent-skill-draft').textContent(), '已儲存');
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const save = await page.locator('#agent-skill-save').boundingBox();
      assert.ok(save.x >= 0 && save.x + save.width <= width && save.y + save.height <= 1000);
      if ([1440, 390].includes(width)) {
        fs.mkdirSync(path.join(root, '.tag_gallery/ui-review'), { recursive: true });
        await page.screenshot({ path: path.join(root, `.tag_gallery/ui-review/agent-skills-${width}.png`), animations: 'disabled' });
      }
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#agent-skill-modal').isHidden(), true);
    assert.deepEqual(errors, []);
    console.log('PASS: agent skill documents, draft retention, save failures, conflicts, reload and responsive editor');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
