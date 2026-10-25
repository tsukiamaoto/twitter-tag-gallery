/* All requests are mocked; no image, skill file or model on disk is changed. */
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
    const errors = [], mutations = [];
    let skills = structuredClone(templates), active = skills[0].id, failSave = false;
    const ai = { running: false, model_loaded: false, disk_cached: false, status_message: '待機中' };
    const schedule = { running_job: false, config: { enabled: false, schedule_times: [], source_dirs: [], history_logs: [] } };
    const gguf = { models: [{ id: skills[0].model_id, filename: path.basename(skills[0].model_id), profile: 'wai_illustrious', valid: true, bytes: 1000 }],
      directory: 'mock/models', selection: { model_id: skills[0].model_id, profile: 'wai_illustrious' }, runtime: { phase: 'unloaded' }, download: {}, missing: [] };
    const assignedModel = 'diffusion_models/custom-wai-illustrious.gguf';
    gguf.models.push({ id: assignedModel, filename: path.basename(assignedModel), profile: 'wai_illustrious', valid: true });
    const agentSkills = ['gallery-deduplication', 'twitter-tag-correlation', 'gallery-artist-identity', 'gallery-publication-tracing']
      .map(id => ({ id, name: id, description: `Agent 工作流程 ${id}`, revision: 'a'.repeat(64), valid: true, kind: 'agent' }));
    await context.route('**/*', async route => {
      const request = route.request(), endpoint = new URL(request.url()).pathname, method = request.method();
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(root, 'static/index.html'), 'utf8') });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {};
      if (endpoint.startsWith('/api/image-skills') && method !== 'GET') {
        mutations.push({ endpoint, method, body: request.postDataJSON() });
        if (failSave && method === 'PUT') return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '儲存失敗，請重試' }) });
        const id = endpoint.split('/')[3];
        if (endpoint.endsWith('/activate')) {
          active = id; data = { prepared: skills.find(skill => skill.id === id), message: '已套用技能' };
        } else if (method === 'DELETE') {
          skills = skills.filter(skill => skill.id !== id); if (active === id) active = null; data = { message: '已封存技能' };
        } else {
          const skill = { ...request.postDataJSON(), id: id || `new-skill-${skills.length}` };
          skills = skills.filter(item => item.id !== skill.id).concat(skill); data = { skill, message: '已儲存技能' };
        }
      } else if (endpoint === '/api/image-skills') data = { skills, active_id: active, directory: 'mock/image_skills' };
      else if (endpoint === '/api/agent-skills') data = { skills: [...agentSkills, ...skills.map(skill => ({ ...skill, kind: 'image-repair' }))] };
      else if (endpoint === '/api/models/gguf') data = gguf;
      else if (endpoint === '/api/runtime') data = { ai, reverse: { running: false }, schedule, active_job: {}, jobs: [], model: {} };
      else if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: ai };
      else if (endpoint === '/api/images') data = { items: [], total: 0 };
      else if (endpoint === '/api/tags') data = { items: [], next_offset: 0, has_more: false };
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/schedule/status') data = schedule;
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://image-skills.test/');
    await page.evaluate(() => openSettings('image-skills'));
    await page.waitForSelector('.image-skill-list-card[data-skill-id="anime-decensor-wai-illustrious"]');
    assert.equal(await page.locator('#image-skill-modal').isHidden(), true, 'Editor modal starts closed on card list view');
    assert.equal(await page.locator('.image-skill-list-card').count(), 1);
    assert.equal(await page.locator('#image-skill-cards > .agent-skill-card').count(), 4, 'All Agent Skills share the same list');
    assert.equal(await page.locator('[role="list"][aria-label="Agent Skill 列表"] > [role="listitem"]').count(), 5, 'Image Skills appear once in the unified catalog');
    assert.equal(await page.locator('.agent-skill-section').count(), 0);
    assert.equal(await page.locator('.image-skill-list-card').first().getAttribute('data-active'), 'true');
    fs.mkdirSync(artifacts, { recursive: true });
    await page.locator('#ai-workspace').screenshot({ path: path.join(artifacts, 'image-skills-cards-desktop.png'), animations: 'disabled' });

    // Clicking a skill card opens the pop-up detail editor modal.
    await page.locator('.image-skill-list-card').first().click();
    assert.equal(await page.locator('#image-skill-modal').isVisible(), true);
    assert.equal(await page.locator('#image-skill-picker').inputValue(), 'anime-decensor-wai-illustrious');
    assert.equal(await page.locator('#image-skill-info').getAttribute('open'), null);
    assert.equal(await page.locator('#image-skill-save').isDisabled(), true);
    assert.equal(await page.locator('#image-skill-model-status').textContent(), '已下載');
    const prompt = await page.locator('#image-skill-prompt').boundingBox();
    const model = await page.locator('.image-skill-model-card').boundingBox();
    const footer = await page.locator('#image-skill-footer').boundingBox();
    assert.ok(prompt.y + prompt.height < footer.y && model.y + model.height < footer.y, 'Prompt and model fit above the modal footer');
    assert.ok(model.x > prompt.x + prompt.width, 'Desktop places model beside the prompt');
    await page.screenshot({ path: path.join(artifacts, 'image-skills-desktop.png'), animations: 'disabled' });

    // Editing locks skill switching, tracks the draft and supports a keyboard save.
    const revised = 'Repair the damaged background. Preserve the original line work and lighting.';
    await page.locator('#image-skill-prompt').fill(revised);
    await page.locator('#image-skill-model').selectOption(assignedModel);
    await page.locator('details').filter({ has: page.locator('#image-skill-instructions') }).locator('summary').click();
    await page.locator('#image-skill-instructions').fill('Preserve the original image. Preview the repaired copy.');
    assert.equal(await page.locator('#image-skill-draft-state').textContent(), '尚未儲存');
    assert.equal(await page.locator('#image-skill-apply').isDisabled(), true);
    assert.equal(await page.locator('#image-skill-picker').isDisabled(), true);
    assert.ok((await page.locator('#image-skill-prompt-count').textContent()).startsWith(String(revised.length)));
    await page.locator('#image-skill-prompt').press('Control+s');
    await page.waitForFunction(() => document.querySelector('#image-skill-draft-state').textContent === '已儲存');
    assert.equal(mutations[0].body.prompt, revised);
    assert.equal(mutations[0].body.model_id, assignedModel);
    assert.equal(mutations[0].body.instructions, 'Preserve the original image. Preview the repaired copy.');
    assert.ok((await page.locator('.image-skill-list-card-meta').first().textContent()).includes(path.basename(assignedModel)));
    assert.equal(await page.locator('#image-skill-apply').isEnabled(), true, 'An edited active skill can be applied again');
    await page.locator('#image-skill-apply').click();
    await page.waitForFunction(() => document.querySelector('#image-skills-feedback').textContent === '已套用技能');
    assert.ok(mutations.some(item => item.endpoint.endsWith('/activate')));

    // Failed saves retain edits; discard restores the saved inference settings.
    await page.locator('details').filter({ has: page.locator('#image-skill-steps') }).locator('summary').click();
    await page.locator('#image-skill-steps').fill('27');
    failSave = true;
    await page.locator('#image-skill-save').click();
    await page.waitForFunction(() => document.querySelector('#image-skills-feedback').textContent.includes('儲存失敗'));
    assert.equal(await page.locator('#image-skill-draft-state').textContent(), '尚未儲存');
    assert.equal(await page.locator('#image-skill-save').isEnabled(), true);
    failSave = false;
    await page.locator('#image-skill-info > summary').click();
    await page.locator('#image-skill-discard').click();
    assert.equal(await page.locator('#image-skill-profile').inputValue(), 'wai_illustrious');
    assert.equal(await page.locator('#image-skill-steps').inputValue(), '20');

    // Going to model management preserves drafts and hides the skill modal on other tabs.
    await page.locator('#image-skill-prompt').fill('Keep this unsaved draft.');
    await page.locator('#image-skills-models').click();
    assert.equal(await page.locator('#image-skill-modal').isHidden(), true);
    assert.equal(await page.locator('#image-skill-footer').isHidden(), true);
    assert.equal(await page.locator('#settings-model').isVisible(), true);
    await page.evaluate(() => switchSettingsTab('image-skills'));
    assert.equal(await page.locator('#image-skill-modal').isVisible(), true);
    assert.equal(await page.locator('#image-skill-prompt').inputValue(), 'Keep this unsaved draft.');
    await page.locator('#image-skill-info > summary').click();
    await page.locator('#image-skill-discard').click();
    await page.locator('#image-skill-modal-close').click();
    assert.equal(await page.locator('#image-skill-modal').isHidden(), true);

    // Required fields inside collapsed details are revealed before browser validation.
    await page.locator('#image-skill-new').click();
    assert.equal(await page.locator('#image-skill-modal').isVisible(), true);
    await page.locator('#image-skill-prompt').fill('Repair a small background patch.');
    await page.locator('#image-skill-info > summary').click();
    await page.locator('#image-skill-save').click();
    assert.equal(await page.locator('#image-skill-name').isVisible(), true);
    await page.locator('#image-skill-name').fill('背景修補');
    await page.locator('#image-skill-save').click();
    await page.waitForFunction(() => document.querySelector('#image-skill-draft-state').textContent === '已儲存');
    assert.equal(skills.at(-1).name, '背景修補');
    assert.equal(await page.locator('.image-skill-list-card').count(), 2, 'New skill appears as a card in the list');
    await page.locator('#image-skill-info > summary').click();
    await page.locator('#image-skill-duplicate').click();
    assert.equal(await page.locator('#image-skill-name').inputValue(), '背景修補 副本');
    assert.equal(await page.locator('#image-skill-prompt').inputValue(), 'Repair a small background patch.');
    await page.locator('#image-skill-discard').click();
    await page.locator('#image-skill-modal-close').click();
    await page.locator('.image-skill-list-card[data-skill-id="anime-decensor-wai-illustrious"]').click();

    for (const width of [1064, 768, 600, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => { document.querySelector('#ai-settings-content').scrollTop = 0; });
      const overflow = await page.evaluate(() => {
        const content = document.querySelector('#ai-settings-content');
        const dialog = document.querySelector('.image-skill-dialog');
        return document.documentElement.scrollWidth > innerWidth + 1 ||
          content.scrollWidth > content.clientWidth + 1 ||
          dialog.scrollWidth > dialog.clientWidth + 1;
      });
      assert.equal(overflow, false, `No horizontal overflow at ${width}px`);
      const save = await page.locator('#image-skill-save').boundingBox();
      assert.ok(save.y >= 0 && save.y + save.height <= 1000, `Save stays in view at ${width}px`);
      if (width === 390) {
        assert.ok(save.width > 90, 'Mobile actions have usable touch targets');
        await page.screenshot({ path: path.join(artifacts, 'image-skills-mobile.png'), animations: 'disabled' });
      }
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#image-skill-modal').isHidden(), true);
    await page.setViewportSize({ width: 390, height: 1000 });
    await page.locator('#ai-workspace').screenshot({ path: path.join(artifacts, 'image-skills-cards-mobile.png'), animations: 'disabled' });
    await page.locator('#workspace-home').click();
    await page.locator('#btn-settings').click();
    assert.equal(await page.locator('#image-skill-footer').isHidden(), true);
    assert.equal(await page.locator('#btn-close-settings').isVisible(), true);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', mutations: mutations.length, browserErrors: errors.length, responsiveWidths: [1440, 1064, 768, 600, 390, 320] }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
