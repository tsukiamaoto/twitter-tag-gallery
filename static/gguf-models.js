/* GGUF controls are independent of the existing ONNX tagging model. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id);
  let latest = null, pending = null, timer = null, acting = false, lastRead = 0;
  const phases = { unloaded: '未載入', loading: '載入中…', loaded: '已載入', failed: '載入失敗' };
  const downloads = { downloading: '下載中', verifying: '校驗中', completed: '下載完成', cancelled: '已停止', interrupted: '已中斷，可續傳', failed: '下載失敗', cancelling: '停止中…' };
  const size = value => `${((value || 0) / 1e9).toFixed(2)} GB`;
  function feedback(message, error = false) {
    byId('gguf-feedback').textContent = message;
    byId('gguf-feedback').classList.toggle('error', error);
  }
  function visible() { return !document.hidden && !byId('settings-model').hidden && byId('schedule-modal').classList.contains('open'); }
  function render(data) {
    const previousPhase = latest?.runtime?.phase;
    latest = data;
    const selector = byId('gguf-model-select');
    const previous = selector.value || data.selection.model_id;
    selector.replaceChildren(new Option(data.models.length ? '請選擇 GGUF 模型' : '尚無 GGUF；下載完成後或放入檔案再掃描', ''));
    for (const model of data.models) {
      const option = new Option(`${model.filename}${model.valid ? ` · ${size(model.bytes)}` : ' · 檔案無效'}`, model.id);
      option.disabled = !model.valid;
      option.dataset.profile = model.profile || '';
      selector.add(option);
    }
    selector.value = data.models.some(m => m.id === previous && m.valid) ? previous : '';
    if (!byId('gguf-profile-select').value || previous === data.selection.model_id) byId('gguf-profile-select').value = data.selection.profile || '';
    byId('gguf-directory').textContent = data.directory;
    const runtime = data.runtime;
    byId('gguf-runtime-state').textContent = phases[runtime.phase] || runtime.phase;
    byId('gguf-runtime-state').dataset.phase = runtime.phase;
    byId('gguf-loaded-model').textContent = runtime.loaded_model ? `目前載入：${runtime.loaded_model.split('/').pop()}` : '目前沒有載入 GGUF 模型';
    byId('gguf-requirements').textContent = !data.selection.model_id ? '先選擇已下載的 GGUF 影像模型。' : data.missing.length ? `目前選擇尚缺：${data.missing.join('、')}` : '目前選擇的必要元件已齊全。';
    byId('gguf-load').disabled = acting || !selector.value || selector.value !== data.selection.model_id || runtime.phase === 'loading' || data.missing.length > 0;
    byId('gguf-unload').disabled = acting || !['loading', 'loaded', 'failed'].includes(runtime.phase);
    byId('gguf-scan').disabled = acting;
    selector.disabled = acting || runtime.phase === 'loading';
    byId('gguf-profile-select').disabled = acting || runtime.phase === 'loading';
    const transfer = data.download;
    const active = ['downloading', 'verifying', 'cancelling'].includes(transfer.phase);
    byId('gguf-download-progress').hidden = !transfer.phase;
    byId('gguf-download-progress').max = Math.max(1, transfer.total_bytes || 1);
    byId('gguf-download-progress').value = transfer.downloaded_bytes || 0;
    byId('gguf-download-state').textContent = transfer.phase ? `${downloads[transfer.phase] || transfer.phase} · ${size(transfer.downloaded_bytes)} / ${size(transfer.total_bytes)}` : '下載方案包含主模型、必要編碼器與 VAE。';
    byId('gguf-download').disabled = acting || active;
    byId('gguf-download-stop').hidden = !active;
    byId('gguf-download-stop').disabled = acting;
    if (runtime.error) feedback(runtime.error, true);
    else if (transfer.error) feedback(transfer.error, true);
    else if (previousPhase === 'failed') feedback('');
    else if (runtime.phase === 'loaded' && previousPhase !== 'loaded') feedback('GGUF 模型已載入。');
  }
  async function refresh() {
    clearTimeout(timer);
    if (pending) return pending;
    pending = app.api('/api/models/gguf').then(data => { lastRead = Date.now(); render(data); })
      .catch(error => feedback(error.message, true)).finally(() => {
        pending = null;
        if (visible()) timer = setTimeout(refresh, 2000);
      });
    return pending;
  }
  async function action(url, payload) {
    if (acting) return;
    acting = true;
    if (latest) render(latest);
    feedback('處理中…');
    try {
      const result = await app.api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) });
      feedback(result.message || '已完成。');
    } catch (error) { feedback(error.message, true); }
    finally { acting = false; await refresh(); }
  }
  function selection() { return { model_id: byId('gguf-model-select').value, profile: byId('gguf-profile-select').value || null }; }
  async function saveSelection() {
    if (!byId('gguf-model-select').value) return;
    await action('/api/models/gguf/select', selection());
  }
  function initialize() {
    const panel = byId('settings-model');
    if (!panel || byId('gguf-model-select')) return;
    const card = document.createElement('section');
    card.className = 'gguf-card';
    card.innerHTML = `<h3>GGUF 影像模型</h3><p class="settings-note">選擇一般影像修補與編修所使用的模型。按載入後才會使用記憶體。</p>
      <label>模型存放位置<code class="gguf-path" id="gguf-directory">讀取中…</code></label>
      <div class="settings-actions"><button class="btn" id="gguf-copy-path">複製資料夾路徑</button><button class="btn" id="gguf-scan">重新掃描</button></div>
      <div class="gguf-grid"><label>影像模型<select class="select-box" id="gguf-model-select"><option value="">讀取中…</option></select></label><label>模型系列<select class="select-box" id="gguf-profile-select"><option value="">自動辨識</option><option value="wai_illustrious">WAI-NSFW-illustrious-SDXL v17.0</option></select></label></div>
      <div class="gguf-status"><span>Vulkan 執行狀態</span><strong id="gguf-runtime-state">讀取中…</strong></div><p class="settings-note" id="gguf-loaded-model"></p><p class="settings-note" id="gguf-requirements"></p>
      <div class="settings-actions"><button class="btn btn-primary" id="gguf-load" disabled>載入選取模型</button><button class="btn" id="gguf-unload" disabled>卸載 GGUF 模型</button><button class="btn" id="gguf-skills">管理修補技能與 prompt</button></div>
      <div class="gguf-download"><p class="settings-note">預設方案：WAI-NSFW-illustrious-SDXL v17.0 Q4_K_M（含 CLIP-L、CLIP-G 與 SDXL VAE）。</p><progress id="gguf-download-progress" hidden aria-label="模型下載進度"></progress><p class="settings-note" id="gguf-download-state"></p><div class="settings-actions"><button class="btn" id="gguf-download">下載／續傳模型方案</button><button class="btn" id="gguf-download-stop" hidden>停止下載</button></div></div>
      <details><summary>如何放入自己的模型</summary><ul class="settings-note"><li>主模型放入 diffusion_models，或直接放在此 GGUF 資料夾。</li><li>CLIP 編碼器放入 text_encoders，VAE 放入 vae。</li><li>按重新掃描後選擇模型系列；必要元件會在上方列出。</li><li>目前支援 WAI-NSFW-illustrious-SDXL v17.0 的 GGUF 影像模型。</li></ul></details><p class="settings-feedback" id="gguf-feedback" role="status" aria-live="polite"></p>`;
    panel.prepend(card);
    byId('gguf-scan').addEventListener('click', refresh);
    byId('settings-tab-model').addEventListener('click', refresh);
    byId('gguf-copy-path').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(byId('gguf-directory').textContent); feedback('已複製模型資料夾路徑。'); }
      catch { feedback('無法複製，請選取上方路徑手動複製。', true); }
    });
    byId('gguf-model-select').addEventListener('change', () => {
      const option = byId('gguf-model-select').selectedOptions[0];
      byId('gguf-profile-select').value = option?.dataset.profile || '';
      void saveSelection();
    });
    byId('gguf-profile-select').addEventListener('change', saveSelection);
    byId('gguf-load').addEventListener('click', () => action('/api/models/gguf/load', selection()));
    byId('gguf-unload').addEventListener('click', () => action('/api/models/gguf/unload'));
    byId('gguf-skills').addEventListener('click', () => window.switchSettingsTab('image-skills'));
    app.on('image-skill:applied', prepared => {
      byId('gguf-model-select').value = prepared.model_id;
      byId('gguf-profile-select').value = prepared.profile;
      byId('gguf-load').disabled = true;
      void refresh();
    });
    byId('gguf-download').addEventListener('click', () => action('/api/models/gguf/download', { profiles: ['wai_illustrious'] }));
    byId('gguf-download-stop').addEventListener('click', () => action('/api/models/gguf/download/stop'));
    app.on('runtime', () => { if (visible() && Date.now() - lastRead > 2000) void refresh(); });
    document.addEventListener('visibilitychange', () => { if (visible()) void refresh(); else clearTimeout(timer); });
    app.use('switchSettingsTab', (next, section) => {
      const result = next(section);
      if (section === 'model') void refresh();
      return result;
    });
    if (visible()) void refresh();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
