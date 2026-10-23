/* Editable application prompt presets. Selecting a skill never transforms a gallery image. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id), api = '/api/image-skills';
  const defaults = { wai_illustrious: { model: 'diffusion_models/WAI-illustrious-SDXL-v170-Q4_K_M.gguf', steps: 20 } };
  let catalog = { skills: [], active_id: null }, models = [], selected = null, dirty = false, busy = false, pending = null;
  const fields = ['name', 'description', 'profile', 'model', 'prompt', 'steps', 'seed'];
  function feedback(message, error = false) { byId('image-skills-feedback').textContent = message; byId('image-skills-feedback').classList.toggle('error', error); }
  function draft() {
    return { name: byId('image-skill-name').value.trim(), description: byId('image-skill-description').value.trim(),
      profile: byId('image-skill-profile').value, model_id: byId('image-skill-model').value,
      prompt: byId('image-skill-prompt').value.trim(), steps: Number(byId('image-skill-steps').value),
      seed: Number(byId('image-skill-seed').value), output_mode: 'copy' };
  }
  function controls() {
    const state = byId('image-skill-draft-state');
    state.textContent = busy ? '處理中…' : dirty ? '尚未儲存' : selected ? '已儲存' : '新技能';
    state.dataset.state = dirty ? 'dirty' : 'saved';
    byId('image-skill-save').disabled = busy || (!!selected && !dirty);
    byId('image-skill-save').textContent = selected ? '儲存修改' : '建立技能';
    byId('image-skill-apply').disabled = busy || dirty || !selected;
    byId('image-skill-apply').title = dirty || !selected ? '請先儲存技能，再套用。' : '使用這份 Prompt 與模型設定';
    byId('image-skill-discard').disabled = busy || !dirty;
    for (const id of ['image-skill-picker', 'image-skill-new', 'image-skill-duplicate']) byId(id).disabled = busy || dirty;
    for (const field of fields) byId(`image-skill-${field}`).disabled = busy;
    byId('image-skill-prompt-count').textContent = `${byId('image-skill-prompt').value.length.toLocaleString()} / 12,000`;
    byId('image-skill-info-summary').textContent = byId('image-skill-name').value.trim() || '名稱與用途';
    const seed = byId('image-skill-seed').value;
    byId('image-skill-advanced-summary').textContent = `${byId('image-skill-steps').value} 步 · ${seed === '-1' ? '隨機種子' : `種子 ${seed}`}`;
    const available = models.some(model => model.id === byId('image-skill-model').value && model.valid);
    byId('image-skill-model-status').textContent = available ? '已下載' : '尚未下載';
    byId('image-skill-model-status').dataset.state = available ? 'available' : 'missing';
  }
  function modelOptions(profile, value) {
    const select = byId('image-skill-model'); select.replaceChildren();
    const defModel = (defaults[profile] || defaults.wai_illustrious).model;
    const candidates = new Set([defModel, ...models.filter(m => m.valid && (m.profile === profile || !m.profile)).map(m => m.id)]);
    if (value) candidates.add(value);
    for (const id of candidates) select.add(new Option(`${id.split('/').pop()}${models.some(m => m.id === id) ? '' : ' · 尚未下載'}`, id));
    select.value = value || defModel;
  }
  function edit(skill) {
    selected = skill?.id || null;
    const value = skill || { name: '', description: '', profile: 'wai_illustrious', model_id: defaults.wai_illustrious.model, prompt: '', steps: 20, seed: 42 };
    for (const field of fields.filter(f => f !== 'model')) byId(`image-skill-${field}`).value = value[field];
    modelOptions(value.profile, value.model_id);
    byId('image-skill-picker').value = selected || '';
    byId('image-skill-info').open = !selected;
    dirty = false; controls();
  }
  function renderList() {
    const picker = byId('image-skill-picker'); picker.replaceChildren(new Option('選擇一份技能，或新增技能', ''));
    for (const skill of catalog.skills) picker.add(new Option(`${skill.name}${skill.id === catalog.active_id ? ' · 套用中' : ''}`, skill.id));
    picker.value = selected || '';
    const active = catalog.skills.find(skill => skill.id === catalog.active_id);
    byId('image-skills-active').textContent = active ? `目前套用：${active.name}` : '目前尚未套用技能';
  }
  async function refresh() {
    if (pending) return pending;
    pending = Promise.all([app.api(api), app.api('/api/models/gguf')]).then(([data, gguf]) => {
      catalog = data; models = gguf.models || []; renderList();
      if (!dirty) edit(catalog.skills.find(s => s.id === selected) || catalog.skills.find(s => s.id === catalog.active_id) || catalog.skills[0]);
    }).catch(error => feedback(error.message, true)).finally(() => { pending = null; });
    return pending;
  }
  async function perform(operation) {
    if (busy) return;
    busy = true; controls(); feedback('處理中…');
    try { await operation(); } catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }
  function initialize() {
    if (byId('settings-image-skills')) return;
    const tab = document.createElement('button'); tab.id = 'settings-tab-image-skills'; tab.className = 'btn settings-tab';
    tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', 'false'); tab.setAttribute('aria-controls', 'settings-image-skills'); tab.tabIndex = -1;
    tab.textContent = '影像修補技能'; tab.addEventListener('click', () => window.switchSettingsTab('image-skills'));
    document.querySelector('.settings-tabs').append(tab);
    const panel = document.createElement('section'); panel.id = 'settings-image-skills'; panel.className = 'settings-panel'; panel.hidden = true;
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id);
    panel.innerHTML = `<div class="image-skills-heading"><h3>影像修補技能</h3><p class="settings-note">以 Prompt 定義修補方式，搭配適合的模型。</p></div>
      <div class="image-skills-toolbar"><label for="image-skill-picker">技能<select id="image-skill-picker" class="select-box"><option>讀取中…</option></select></label><button class="btn" id="image-skill-new"><span aria-hidden="true">＋</span> 新增技能</button></div>
      <p class="image-skills-current settings-note" id="image-skills-active"></p>
      <form id="image-skill-form" class="image-skill-editor">
        <div class="image-skill-workspace">
          <section class="image-skill-card image-skill-prompt-card" aria-labelledby="image-skill-prompt-label">
            <div class="image-skill-card-heading"><label for="image-skill-prompt" id="image-skill-prompt-label">修補指示 <span>Prompt</span></label><span class="image-skill-badge">主要設定</span></div>
            <p class="settings-note" id="image-skill-prompt-help">描述要去除的馬賽克、白邊／黑條或模糊區域，以及要補強的線條、膚質與光影細節。</p>
            <textarea id="image-skill-prompt" required maxlength="12000" rows="10" spellcheck="false" aria-describedby="image-skill-prompt-help" placeholder="例如：去除遮罩區域的馬賽克、白邊與模糊，補強清晰的連續線條、自然膚質、光影漸層與細節，並保留周圍未遮罩區域。"></textarea>
            <div class="image-skill-prompt-meta"><span>支援中文與英文</span><span id="image-skill-prompt-count"></span></div>
          </section>
          <section class="image-skill-card image-skill-model-card" aria-labelledby="image-skill-model-heading">
            <div class="image-skill-card-heading"><h4 id="image-skill-model-heading">模型 <span>Model</span></h4><span class="image-skill-badge" id="image-skill-model-status"></span></div>
            <label>模型系列<select class="select-box" id="image-skill-profile"><option value="wai_illustrious">WAI-NSFW-illustrious-SDXL v17.0</option></select></label>
            <label>GGUF 模型檔<select class="select-box" id="image-skill-model"></select></label>
            <div class="image-skill-model-help"><p class="settings-note">套用技能後，至 AI 模型頁載入模型。</p><button class="btn btn-text" type="button" id="image-skills-models">管理 GGUF 模型 <span aria-hidden="true">↗</span></button></div>
          </section>
        </div>
        <details class="image-skill-details" id="image-skill-info"><summary><span>技能資訊</span><span class="image-skill-detail-summary" id="image-skill-info-summary"></span></summary>
          <div class="image-skill-details-body"><label>技能名稱<input class="search-input" id="image-skill-name" required maxlength="100" placeholder="為這份技能命名"></label><label><span>用途說明 <span class="image-skill-optional">選填</span></span><input class="search-input" id="image-skill-description" maxlength="1000" placeholder="簡述適合使用的情境"></label>
          <div class="settings-actions image-skill-management"><button class="btn" type="button" id="image-skill-duplicate">複製為新技能</button><button class="btn" type="button" id="image-skill-discard">放棄修改</button></div></div>
        </details>
        <details class="image-skill-details"><summary><span>進階設定</span><span class="image-skill-detail-summary" id="image-skill-advanced-summary"></span></summary>
          <div class="image-skill-details-body"><div class="image-skills-grid"><label>推論步數<input class="search-input" type="number" id="image-skill-steps" min="1" max="100" required></label><label>隨機種子<input class="search-input" type="number" id="image-skill-seed" min="-1" max="2147483647" required><span class="settings-note">設為 -1 時，每次使用隨機種子。</span></label></div>
          <p class="settings-note">修補結果另存副本。</p></div>
        </details>
      </form>`;
    document.querySelector('.settings-content').append(panel);
    const footer = document.createElement('div'); footer.id = 'image-skill-footer'; footer.className = 'image-skill-footer'; footer.hidden = true;
    footer.innerHTML = `<div class="image-skill-footer-info"><span id="image-skill-draft-state" role="status" aria-live="polite"></span><p id="image-skills-feedback" class="settings-feedback" role="status" aria-live="polite"></p><span class="settings-note">套用後不會立即處理圖片</span></div><div class="settings-actions"><button class="btn" type="button" id="image-skill-apply">套用技能</button><button class="btn btn-primary" type="submit" form="image-skill-form" id="image-skill-save">儲存修改</button></div>`;
    document.querySelector('.settings-footer').append(footer);
    byId('image-skills-models').addEventListener('click', () => window.switchSettingsTab('model'));
    byId('image-skill-picker').addEventListener('change', () => { edit(catalog.skills.find(s => s.id === byId('image-skill-picker').value)); feedback(''); });
    byId('image-skill-new').addEventListener('click', () => { edit(null); feedback('填寫 Prompt、模型與技能名稱後建立。'); byId('image-skill-prompt').focus(); });
    byId('image-skill-duplicate').addEventListener('click', () => { const value = { ...draft(), name: `${draft().name} 副本`.slice(0,100) }; edit(value); dirty = true; controls(); });
    byId('image-skill-discard').addEventListener('click', () => { edit(catalog.skills.find(s => s.id === selected)); feedback('已放棄未儲存的修改。'); });
    byId('image-skill-form').addEventListener('input', () => { dirty = true; feedback(''); controls(); });
    byId('image-skill-form').addEventListener('invalid', event => {
      for (let parent = event.target.parentElement; parent && parent !== panel; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }, true);
    panel.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.isComposing && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (!byId('image-skill-save').disabled) byId('image-skill-form').requestSubmit();
      }
    });
    byId('image-skill-profile').addEventListener('change', () => { const profile = byId('image-skill-profile').value; modelOptions(profile); byId('image-skill-steps').value = defaults[profile].steps; dirty = true; controls(); });
    byId('image-skill-form').addEventListener('submit', event => {
      event.preventDefault(); void perform(async () => {
        const response = await app.api(selected ? `${api}/${selected}` : api, { method: selected ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft()) });
        selected = response.skill.id; dirty = false; await refresh(); feedback(response.message);
      });
    });
    byId('image-skill-apply').addEventListener('click', () => perform(async () => {
      const response = await app.api(`${api}/${selected}/activate`, { method: 'POST' });
      catalog.active_id = selected; renderList(); app.emit('image-skill:applied', response.prepared); feedback(response.message);
    }));
    app.use('switchSettingsTab', (next, section) => {
      const result = next(section), active = section === 'image-skills';
      footer.hidden = !active;
      byId('settings-footer-note').hidden = active;
      if (active) { void refresh(); byId('btn-close-settings').hidden = true; }
      return result;
    });
    edit(null);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
