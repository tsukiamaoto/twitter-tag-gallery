/* Isolated browser verification: every request is mocked, including model actions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.tag_gallery', 'ui-review');
fs.mkdirSync(artifacts, { recursive: true });
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], writes = [];
    const ai = { running: false, model_loaded: false, disk_cached: true, disk_size_mb: 650, processed_count: 0, total_target: 100, status_message: '待機中' };
    const schedule = { running_job: false, processed_count: 0, total_target: 100, config: { enabled: false, schedule_times: ['03:00'], source_dirs: [], history_logs: [] } };
    let failStart = false, busyStart = false, failModel = false;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (endpoint.startsWith('/static/')) return route.fulfill({ contentType: endpoint.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, endpoint), 'utf8') });
      let data = {}, status = 200;
      if (request.method() === 'POST') {
        writes.push({ endpoint, params: Object.fromEntries(url.searchParams) });
        if (endpoint === '/api/ai/start') {
          if (failStart) { status = 503; data = { detail: '啟動失敗（模擬）' }; }
          else if (busyStart) data = { status: 'already_running', message: '已有背景任務正在執行（模擬）' };
          else { ai.running = true; ai.processed_count = 0; ai.status_message = '正在分析圖片'; data = { status: 'started' }; }
        } else if (endpoint === '/api/ai/load') {
          if (failModel) { status = 500; data = { detail: '模型載入失敗（模擬）' }; }
          else { ai.model_loaded = true; ai.disk_cached = true; ai.provider = 'DmlExecutionProvider'; data = { status: 'loaded', message: '模型已載入（模擬）' }; }
        } else if (endpoint === '/api/ai/unload') {
          ai.model_loaded = false;
          if (url.searchParams.get('delete_disk_files') === 'true') ai.disk_cached = false;
          data = { status: 'unloaded', message: '模型已卸載（模擬）' };
        } else if (endpoint === '/api/ai/stop' || endpoint === '/api/reverse_search/stop') {
          ai.running = false; ai.status_message = '已停止'; data = { status: 'stopping' };
        } else throw new Error(`Unexpected mutation: ${endpoint}`);
      } else if (endpoint === '/api/stats') data = { total_images: 0, root_unorganized: 0, folder_organized: 0, ai_tagged: 0, suggested_folder_count: 0, ai_status: { ...ai } };
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = {items:[], next_offset:0, has_more:false};
      else if (endpoint === '/api/images') data = { items: [], total: 0 };
      else if (endpoint === '/api/runtime') data = { ai: { ...ai }, reverse: { running: false }, schedule: { ...schedule }, model: { busy: false }, active_job: {}, jobs: [] };
      else if (endpoint === '/api/ai/status') data = { ...ai };
      else if (endpoint === '/api/reverse_search/status') data = { running: false, processed_count: 0, status_message: '待機中' };
      else if (endpoint === '/api/schedule/status') data = { ...schedule };
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForSelector('#shell-reset-empty');
    await page.getByRole('button', { name: '智慧標籤', exact: true }).click();
    assert.equal(await page.locator('#ai-modal').isVisible(), true);
    assert.equal(await page.locator('#smart-advanced').getAttribute('open'), null);
    assert.equal(await page.locator('#ai-modal #btn-modal-load-vram').count(), 0);
    assert.equal(await page.locator('#ai-modal #ai-auto-unload').count(), 0);
    assert.equal(await page.locator('#btn-top-unload-model').count(), 0);
    assert.equal(await page.locator('#ai-scope').inputValue(), 'all_untagged');
    for (const id of ['ai-step-local', 'ai-step-rev', 'ai-auto-move']) assert.equal(await page.locator(`#${id}`).isChecked(), true);
    assert.equal(await page.locator('#ai-step-decensor').isChecked(), false);
    assert.equal(await page.locator('#ai-modal-live-box').isVisible(), false);
    await page.screenshot({ path: path.join(artifacts, 'smart-tagging-desktop.png') });
    await page.locator('#btn-start-smart-tag').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'smart-model-settings');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'btn-start-smart-tag');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#ai-modal').isVisible(), false);
    assert.ok((await page.evaluate(() => document.activeElement.getAttribute('onclick'))).includes('openAiModal'));

    await page.getByRole('button', { name: '智慧標籤', exact: true }).click();
    await page.locator('#smart-model-settings').click();
    assert.equal(await page.locator('#ai-modal').isVisible(), false);
    assert.equal(await page.locator('#settings-model').isVisible(), true);
    assert.equal(await page.locator('#ai-category-list [aria-selected="true"]').count(), 1);
    assert.equal(await page.locator('#btn-modal-load-vram').isEnabled(), true);
    assert.equal(await page.locator('#btn-modal-unload-vram').isDisabled(), true);
    assert.equal(await page.locator('#settings-model #ai-auto-unload').count(), 1);
    await page.locator('#ai-auto-unload').uncheck();
    await page.screenshot({ path: path.join(artifacts, 'model-settings.png') });
    await page.locator('#ai-return-smart').click();
    assert.equal(await page.locator('#ai-modal').isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'smart-model-settings');
    assert.equal(writes.length, 0, 'Opening dialogs and changing preferences must not run tasks or load models');
    await page.keyboard.press('Escape');
    assert.ok((await page.evaluate(() => document.activeElement.getAttribute('onclick'))).includes('openAiModal'), 'Closing the returned dialog restores its original opener');
    await page.getByRole('button', { name: '智慧標籤', exact: true }).click();

    await page.locator('#ai-scope').selectOption('ai_low_conf');
    await page.locator('#ai-limit').fill('13');
    await page.locator('#ai-step-rev').uncheck();
    assert.equal(await page.locator('#ai-step-sem').isChecked(), false);
    assert.equal(await page.locator('#ai-step-sem').isVisible(), false);
    await page.locator('#ai-auto-move').uncheck();
    await page.locator('#smart-advanced > summary').click();
    assert.equal(await page.locator('#ai-gen-thresh').isVisible(), false);
    await page.locator('#ai-art-thresh').fill('0.99');
    await page.locator('#btn-start-smart-tag').click();
    assert.equal(writes.length, 0, 'Invalid numeric parameters cannot launch a job');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'ai-art-thresh');
    await page.locator('#ai-art-thresh').fill('0.70');
    assert.equal(await page.locator('#rev-apikey').getAttribute('type'), 'password');
    await page.locator('#btn-start-smart-tag').click();
    await page.waitForFunction(() => document.querySelector('#ai-modal-live-box').hidden === false);
    assert.deepEqual(writes[0], { endpoint: '/api/ai/start', params: {
      scope: 'ai_low_conf', limit: '13', general_thresh: '0.38', artist_thresh: '0.70', run_local_ai: 'true', run_reverse_search_calibrate: 'false', run_semantic_themes: 'false', run_decensor: 'false',
      reverse_search_limit: '30', min_similarity: '72', auto_move_to_artist_folder: 'false', auto_unload: 'false', decensor_save_mode: 'new_copy',
    } });
    assert.equal(await page.locator('#btn-start-smart-tag').isDisabled(), true);
    assert.equal(await page.locator('#ai-scope').isDisabled(), true);
    assert.equal(await page.locator('#ai-modal-live-pct').textContent(), '0%');
    ai.processed_count = 25;
    await page.evaluate(() => checkAiStatus());
    assert.equal(await page.locator('#ai-modal-live-pct').textContent(), '25%');
    assert.equal(await page.locator('.smart-progress-track').getAttribute('aria-valuenow'), '25');
    await page.locator('#smart-model-settings').click();
    for (const id of ['btn-modal-load-vram', 'btn-modal-unload-vram', 'btn-modal-unload-disk']) assert.equal(await page.locator(`#${id}`).isDisabled(), true);
    assert.equal(await page.locator('#shell-model-busy').isVisible(), true);
    await page.locator('#ai-return-smart').click();
    assert.equal(await page.locator('#ai-scope').inputValue(), 'ai_low_conf');
    assert.equal(await page.locator('#ai-limit').inputValue(), '13');
    await page.locator('#btn-modal-stop-task').click();
    await page.waitForFunction(() => !document.querySelector('#btn-start-smart-tag').disabled);
    assert.ok(writes.some(request => request.endpoint === '/api/ai/stop'));
    assert.ok(writes.some(request => request.endpoint === '/api/reverse_search/stop'));

    schedule.running_job = true; schedule.processed_count = 90;
    await page.evaluate(() => checkAiStatus());
    assert.equal(await page.locator('#btn-start-smart-tag').isDisabled(), true);
    assert.equal(await page.locator('#btn-modal-stop-task').isVisible(), false);
    assert.equal(await page.locator('#ai-modal-live-pct').textContent(), '90%');
    assert.equal(await page.locator('#ai-modal-live-msg').textContent(), '排程自動處理中');
    await page.screenshot({ path: path.join(artifacts, 'smart-tagging-running.png') });
    schedule.running_job = false;
    await page.evaluate(() => checkAiStatus());
    await page.locator('#smart-model-settings').click();
    await page.locator('#btn-modal-load-vram').click();
    await page.waitForFunction(() => document.querySelector('#btn-modal-unload-vram').disabled === false);
    assert.equal(await page.locator('#ai-modal-model-state').textContent(), '已載入');
    await page.locator('#btn-modal-unload-vram').click();
    await page.waitForFunction(() => document.querySelector('#btn-modal-load-vram').disabled === false);
    assert.equal(writes.filter(request => request.endpoint === '/api/ai/unload').at(-1).params.delete_disk_files, 'false');
    await page.locator('#settings-model details').filter({has:page.locator('#shell-model-cache')}).locator('summary').click();
    const beforeCancel = writes.length;
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#btn-modal-unload-disk').click();
    assert.equal(writes.length, beforeCancel, 'Canceling cache deletion does not send a request');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#btn-modal-unload-disk').click();
    await page.waitForFunction(() => document.querySelector('#shell-model-cache').textContent === '無快取');
    failModel = true;
    await page.locator('#btn-modal-load-vram').click();
    await page.waitForFunction(() => document.querySelector('#shell-model-feedback').classList.contains('error'));
    assert.ok((await page.locator('#shell-model-feedback').textContent()).includes('模型載入失敗'));
    assert.equal(await page.locator('#btn-modal-load-vram').isEnabled(), true);
    failModel = false;

    await page.locator('#btn-settings').click();
    await page.locator('#settings-tab-browse').focus();
    const lastTab = page.locator('.settings-tabs [role="tab"]').last();
    await page.keyboard.press('End');
    assert.equal(await lastTab.getAttribute('aria-selected'), 'true');
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#settings-browse').isVisible(), true);
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.locator(`#${await lastTab.getAttribute('aria-controls')}`).isVisible(), true);
    await page.keyboard.press('Escape');
    await page.locator('#ai-return-smart').click();
    assert.equal(await page.locator('#ai-modal').isVisible(), true);
    await page.keyboard.press('Escape');
    await page.reload();
    await page.waitForSelector('#shell-reset-empty');
    assert.equal(await page.locator('#ai-auto-unload').isChecked(), false, 'Model preference survives reload');

    await page.getByRole('button', { name: '智慧標籤', exact: true }).click();
    failStart = true;
    await page.locator('#btn-start-smart-tag').click();
    await page.waitForFunction(() => document.querySelector('#smart-feedback').textContent.includes('啟動失敗'));
    assert.ok((await page.locator('#smart-feedback').textContent()).includes('啟動失敗'));
    assert.equal(await page.locator('#btn-start-smart-tag').isEnabled(), true);
    failStart = false; busyStart = true;
    await page.locator('#btn-start-smart-tag').click();
    await page.waitForFunction(() => document.querySelector('#smart-feedback').textContent.includes('已有背景任務'));
    assert.ok((await page.locator('#smart-feedback').textContent()).includes('已有背景任務'));
    busyStart = false;
    const widths = [1440, 1024, 768, 600, 390, 320];
    for (const width of widths) {
      await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
      const bounds = await page.locator('.smart-dialog').boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width, `Dialog fits ${width}px`);
      assert.equal(await page.locator('#smart-close').isVisible(), true);
      assert.equal(await page.locator('#btn-start-smart-tag').isVisible(), true);
      await page.locator('#smart-model-settings').click();
      const settingsBounds = await page.locator('#ai-workspace').boundingBox();
      assert.ok(settingsBounds.x >= 0 && settingsBounds.x + settingsBounds.width <= width, `Model settings fit ${width}px`);
      assert.equal(await page.locator('#ai-return-smart').isVisible(), true);
      await page.locator('#ai-return-smart').click();
      if (width === 390) await page.screenshot({ path: path.join(artifacts, 'smart-tagging-mobile.png') });
    }
    await page.locator('#smart-advanced > summary').click();
    await page.locator('#rev-apikey').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#smart-close-footer').isVisible(), true, 'Footer stays reachable while advanced fields scroll');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', widths, mockedActions: writes.length, browserErrors: errors.length, screenshots: artifacts }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
