/* Editable application prompt presets. Selecting a skill never transforms a gallery image. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id), api = '/api/image-skills';
  const defaults = { wai_illustrious: { label: 'WAI-NSFW-illustrious-SDXL v17.0', model: 'diffusion_models/WAI-illustrious-SDXL-v170-Q4_K_M.gguf', steps: 20 } };
  let catalog = { skills: [], active_id: null }, models = [], selected = null, previousSelected = null;
  let dirty = false, busy = false, pending = null, loaded = false, modalWanted = false, returnFocus = null;
  const fields = ['name', 'description', 'profile', 'model', 'prompt', 'steps', 'seed'];

  function feedback(message, error = false) {
    for (const id of ['image-skills-feedback', 'image-skills-page-feedback']) {
      const node = byId(id);
      if (!node) continue;
      node.textContent = message;
      node.classList.toggle('error', error);
    }
  }

  function draft() {
    return {
      name: byId('image-skill-name').value.trim(),
      description: byId('image-skill-description').value.trim(),
      profile: byId('image-skill-profile').value,
      model_id: byId('image-skill-model').value,
      prompt: byId('image-skill-prompt').value.trim(),
      steps: Number(byId('image-skill-steps').value),
      seed: Number(byId('image-skill-seed').value),
      output_mode: 'copy',
    };
  }

  function modelAvailable(modelId) {
    return models.some(model => model.id === modelId && model.valid);
  }

  function syncModalVisibility() {
    const modal = byId('image-skill-modal');
    if (!modal) return;
    const visible = modalWanted && Boolean(app.runtime.navigation?.isVisible('image-skills'));
    modal.hidden = !visible;
    modal.classList.toggle('open', visible);
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
    const currentName = byId('image-skill-name').value.trim();
    byId('image-skill-modal-title').textContent = currentName || (selected ? '修正技能詳細資訊' : '新增影像修補技能');
    byId('image-skill-prompt-count').textContent = `${byId('image-skill-prompt').value.length.toLocaleString()} / 12,000`;
    byId('image-skill-info-summary').textContent = currentName || '名稱與用途';
    const seed = byId('image-skill-seed').value;
    byId('image-skill-advanced-summary').textContent = `${byId('image-skill-steps').value} 步 · ${seed === '-1' ? '隨機種子' : `種子 ${seed}`}`;
    const available = modelAvailable(byId('image-skill-model').value);
    byId('image-skill-model-status').textContent = available ? '已下載' : '尚未下載';
    byId('image-skill-model-status').dataset.state = available ? 'available' : 'missing';
    for (const button of document.querySelectorAll('.image-skill-card-apply')) {
      const id = button.dataset.skillId;
      button.disabled = busy || (dirty && id === selected);
    }
  }

  function modelOptions(profile, value) {
    const select = byId('image-skill-model');
    select.replaceChildren();
    const defModel = (defaults[profile] || defaults.wai_illustrious).model;
    const candidates = new Set([defModel, ...models.filter(m => m.valid && (m.profile === profile || !m.profile)).map(m => m.id)]);
    if (value) candidates.add(value);
    for (const id of candidates) {
      select.add(new Option(`${id.split('/').pop()}${models.some(m => m.id === id) ? '' : ' · 尚未下載'}`, id));
    }
    select.value = value || defModel;
  }

  function edit(skill) {
    selected = skill?.id || null;
    if (selected) previousSelected = selected;
    const value = skill || { name: '', description: '', profile: 'wai_illustrious', model_id: defaults.wai_illustrious.model, prompt: '', steps: 20, seed: 42 };
    for (const field of fields.filter(f => f !== 'model')) byId(`image-skill-${field}`).value = value[field];
    modelOptions(value.profile, value.model_id);
    byId('image-skill-picker').value = selected || '';
    byId('image-skill-info').open = !selected;
    dirty = false;
    controls();
    renderCards();
  }

  function openModal(skill, trigger = document.activeElement) {
    if (trigger && trigger !== document.body) returnFocus = trigger;
    const sameTarget = skill === undefined || (skill ? skill.id === selected : !selected);
    if (!dirty || !sameTarget) edit(skill);
    else controls();
    modalWanted = true;
    syncModalVisibility();
    const focusTarget = !selected ? byId('image-skill-prompt') : byId('image-skill-modal-close');
    focusTarget?.focus({ preventScroll: true });
  }

  function closeModal() {
    modalWanted = false;
    syncModalVisibility();
    renderCards();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }

  function renderCards() {
    const container = byId('image-skill-cards');
    if (!container) return;
    container.replaceChildren();
    if (!loaded && !catalog.skills.length) {
      const loadingCard = document.createElement('div');
      loadingCard.className = 'image-skill-empty';
      loadingCard.textContent = '正在讀取技能列表…';
      container.append(loadingCard);
      return;
    }
    if (!catalog.skills.length) {
      const empty = document.createElement('div');
      empty.className = 'image-skill-empty';
      empty.textContent = '目前尚無影像修補技能，請點選右上角「新增技能」建立。';
      container.append(empty);
      return;
    }
    for (const skill of catalog.skills) {
      const isActive = skill.id === catalog.active_id;
      const isDirty = dirty && skill.id === selected;
      const available = modelAvailable(skill.model_id);
      const card = document.createElement('article');
      card.className = 'image-skill-list-card';
      card.setAttribute('role', 'listitem');
      card.dataset.skillId = skill.id;
      card.dataset.active = String(isActive);

      const top = document.createElement('div');
      top.className = 'image-skill-list-card-top';
      const heading = document.createElement('div');
      heading.className = 'image-skill-list-card-heading';
      const category = document.createElement('span');
      category.className = 'image-skill-list-card-category';
      category.textContent = '影像修補技能';
      const title = document.createElement('h4');
      title.className = 'image-skill-list-card-title';
      title.textContent = skill.name;
      heading.append(category, title);

      const badges = document.createElement('div');
      badges.className = 'image-skill-list-card-badges';
      if (isActive) {
        const activeBadge = document.createElement('span');
        activeBadge.className = 'image-skill-badge';
        activeBadge.dataset.state = 'active';
        activeBadge.textContent = '套用中';
        badges.append(activeBadge);
      }
      if (isDirty) {
        const dirtyBadge = document.createElement('span');
        dirtyBadge.className = 'image-skill-badge';
        dirtyBadge.dataset.state = 'dirty';
        dirtyBadge.textContent = '尚未儲存';
        badges.append(dirtyBadge);
      }
      const modelBadge = document.createElement('span');
      modelBadge.className = 'image-skill-badge';
      modelBadge.dataset.state = available ? 'available' : 'missing';
      modelBadge.textContent = available ? '已下載' : '尚未下載';
      badges.append(modelBadge);
      top.append(heading, badges);

      const desc = document.createElement('p');
      desc.className = 'image-skill-list-card-desc';
      desc.textContent = skill.description || '未填寫用途說明';

      const promptBox = document.createElement('div');
      promptBox.className = 'image-skill-list-card-prompt';
      const promptLabel = document.createElement('span');
      promptLabel.className = 'image-skill-list-card-prompt-label';
      promptLabel.textContent = '修補指示 Prompt';
      const promptText = document.createElement('p');
      promptText.className = 'image-skill-list-card-prompt-text';
      promptText.textContent = skill.prompt;
      promptBox.append(promptLabel, promptText);

      const meta = document.createElement('div');
      meta.className = 'image-skill-list-card-meta';
      const modelInfo = document.createElement('span');
      const profileLabel = defaults[skill.profile]?.label || skill.profile;
      modelInfo.textContent = `${profileLabel} · ${skill.model_id.split('/').pop()}`;
      const stepInfo = document.createElement('span');
      stepInfo.textContent = `${skill.steps} 步 · ${Number(skill.seed) === -1 ? '隨機種子' : `種子 ${skill.seed}`}`;
      meta.append(modelInfo, stepInfo);

      const actions = document.createElement('div');
      actions.className = 'image-skill-list-card-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn image-skill-card-edit';
      editBtn.dataset.skillId = skill.id;
      editBtn.textContent = '詳細資訊與修正';
      editBtn.addEventListener('click', event => {
        event.stopPropagation();
        if (dirty && selected !== skill.id) {
          feedback('請先在視窗內儲存或放棄目前的技能修改，再切換其他技能。', true);
          openModal(undefined, editBtn);
          return;
        }
        feedback('');
        openModal(skill, editBtn);
      });

      const applyBtn = document.createElement('button');
      applyBtn.type = 'button';
      applyBtn.className = `btn ${isActive ? '' : 'btn-primary'} image-skill-card-apply`;
      applyBtn.dataset.skillId = skill.id;
      applyBtn.disabled = busy || isDirty;
      applyBtn.textContent = isActive ? '重新套用' : '套用技能';
      applyBtn.addEventListener('click', event => {
        event.stopPropagation();
        void activateSkill(skill.id);
      });

      actions.append(editBtn, applyBtn);
      card.append(top, desc, promptBox, meta, actions);
      card.addEventListener('click', event => {
        if (event.target.closest('button')) return;
        editBtn.click();
      });
      container.append(card);
    }
  }

  function renderList() {
    const picker = byId('image-skill-picker');
    picker.replaceChildren(new Option('選擇一份技能，或新增技能', ''));
    for (const skill of catalog.skills) {
      picker.add(new Option(`${skill.name}${skill.id === catalog.active_id ? ' · 套用中' : ''}`, skill.id));
    }
    picker.value = selected || '';
    const active = catalog.skills.find(skill => skill.id === catalog.active_id);
    byId('image-skills-active').textContent = active ? `目前套用：${active.name}` : '目前尚未套用技能';
    renderCards();
  }

  async function refresh() {
    if (pending) return pending;
    pending = Promise.all([app.api(api), app.api('/api/models/gguf')]).then(([data, gguf]) => {
      loaded = true;
      catalog = data;
      models = gguf.models || [];
      renderList();
      if (!dirty) {
        edit(catalog.skills.find(s => s.id === selected) || catalog.skills.find(s => s.id === catalog.active_id) || catalog.skills[0]);
      } else {
        controls();
      }
    }).catch(error => feedback(error.message, true)).finally(() => { pending = null; });
    return pending;
  }

  async function perform(operation) {
    if (busy) return;
    busy = true;
    controls();
    feedback('處理中…');
    try { await operation(); }
    catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }

  function activateSkill(skillId) {
    if (!skillId) return;
    return perform(async () => {
      const response = await app.api(`${api}/${skillId}/activate`, { method: 'POST' });
      catalog.active_id = skillId;
      renderList();
      app.emit('image-skill:applied', response.prepared);
      feedback(response.message);
    });
  }

  function initialize() {
    if (byId('settings-image-skills')) return;
    const tab = document.createElement('button');
    tab.id = 'settings-tab-image-skills';
    tab.className = 'btn settings-tab';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', 'false');
    tab.setAttribute('aria-controls', 'settings-image-skills');
    tab.tabIndex = -1;
    tab.textContent = '影像修補技能';
    tab.addEventListener('click', () => window.switchSettingsTab('image-skills'));
    document.querySelector('.settings-tabs').append(tab);

    const panel = document.createElement('section');
    panel.id = 'settings-image-skills';
    panel.className = 'settings-panel';
    panel.hidden = true;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    panel.innerHTML = `
      <div class="image-skills-header">
        <div class="image-skills-heading">
          <h3>影像修補技能</h3>
          <p class="settings-note">以 Prompt 定義修補方式，搭配適合的模型。點選技能卡片即可跳出視窗修正詳細設定。</p>
          <p class="image-skills-current settings-note" id="image-skills-active">讀取中…</p>
        </div>
        <div class="image-skills-toolbar">
          <button class="btn btn-primary" type="button" id="image-skill-new"><span aria-hidden="true">＋</span> 新增技能</button>
        </div>
      </div>
      <p id="image-skills-page-feedback" class="settings-feedback" role="status" aria-live="polite"></p>
      <div id="image-skill-cards" class="image-skill-cards" role="list" aria-label="影像修補技能卡片列表"></div>`;
    document.querySelector('.settings-content').append(panel);

    const modal = document.createElement('div');
    modal.id = 'image-skill-modal';
    modal.className = 'modal-backdrop';
    modal.hidden = true;
    modal.innerHTML = `
      <div class="dialog-box image-skill-dialog" role="dialog" aria-modal="true" aria-labelledby="image-skill-modal-title" tabindex="-1">
        <div class="image-skill-modal-header">
          <div class="image-skill-modal-heading">
            <p class="image-skill-modal-eyebrow">Skill 管理 · 影像修補技能</p>
            <h3 id="image-skill-modal-title">修正技能詳細資訊</h3>
          </div>
          <div class="image-skill-modal-header-actions">
            <label class="image-skill-modal-picker" for="image-skill-picker" hidden>
              <span>技能</span>
              <select id="image-skill-picker" class="select-box" aria-label="選擇技能"><option value="">讀取中…</option></select>
            </label>
            <button class="btn" type="button" id="image-skill-modal-close" aria-label="關閉技能詳細設定">✕</button>
          </div>
        </div>
        <div class="image-skill-modal-body">
          <form id="image-skill-form" class="image-skill-editor">
            <div class="image-skill-workspace">
              <section class="image-skill-card image-skill-prompt-card" aria-labelledby="image-skill-prompt-label">
                <div class="image-skill-card-heading"><label for="image-skill-prompt" id="image-skill-prompt-label">修補指示 <span>Prompt</span></label><span class="image-skill-badge">主要設定</span></div>
                <p class="settings-note" id="image-skill-prompt-help">描述要去除的馬賽克、白邊／黑條或模糊區域，以及要補強的線條、膚質與光影細節。</p>
                <textarea id="image-skill-prompt" required maxlength="12000" rows="8" spellcheck="false" aria-describedby="image-skill-prompt-help" placeholder="例如：去除遮罩區域的馬賽克、白邊與模糊，補強清晰的連續線條、自然膚質、光影漸層與細節，並保留周圍未遮罩區域。"></textarea>
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
          </form>
        </div>
        <div id="image-skill-footer" class="image-skill-footer">
          <div class="image-skill-footer-info"><span id="image-skill-draft-state" role="status" aria-live="polite"></span><p id="image-skills-feedback" class="settings-feedback" role="status" aria-live="polite"></p><span class="settings-note">套用後不會立即處理圖片</span></div>
          <div class="settings-actions"><button class="btn" type="button" id="image-skill-close-footer">關閉</button><button class="btn" type="button" id="image-skill-apply">套用技能</button><button class="btn btn-primary" type="submit" form="image-skill-form" id="image-skill-save">儲存修改</button></div>
        </div>
      </div>`;
    document.body.append(modal);

    window.openImageSkillModal = skillId => {
      const skill = skillId ? catalog.skills.find(item => item.id === skillId) : null;
      openModal(skill);
    };
    window.closeImageSkillModal = closeModal;

    modal.addEventListener('click', event => {
      if (event.target === modal) closeModal();
    });
    byId('image-skill-modal-close').addEventListener('click', closeModal);
    byId('image-skill-close-footer').addEventListener('click', closeModal);
    byId('image-skills-models').addEventListener('click', () => window.switchSettingsTab('model'));
    byId('image-skill-picker').addEventListener('change', () => {
      edit(catalog.skills.find(s => s.id === byId('image-skill-picker').value));
      feedback('');
    });
    byId('image-skill-new').addEventListener('click', event => {
      edit(null);
      feedback('填寫 Prompt、模型與技能名稱後建立。');
      openModal(null, event.currentTarget);
      byId('image-skill-prompt').focus();
    });
    byId('image-skill-duplicate').addEventListener('click', () => {
      const origin = selected;
      const value = { ...draft(), name: `${draft().name} 副本`.slice(0, 100) };
      edit(value);
      previousSelected = origin;
      dirty = true;
      controls();
      renderCards();
    });
    byId('image-skill-discard').addEventListener('click', () => {
      const targetId = selected || previousSelected;
      edit(catalog.skills.find(s => s.id === targetId) || catalog.skills[0] || null);
      feedback('已放棄未儲存的修改。');
    });
    byId('image-skill-form').addEventListener('input', () => {
      dirty = true;
      feedback('');
      controls();
      renderCards();
    });
    byId('image-skill-form').addEventListener('invalid', event => {
      for (let parent = event.target.parentElement; parent && parent !== modal; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }, true);
    window.addEventListener('keydown', event => {
      if (modal.hidden) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeModal();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.isComposing && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (!byId('image-skill-save').disabled) byId('image-skill-form').requestSubmit();
        return;
      }
      if (event.key === 'Tab') {
        const focusable = [...modal.querySelectorAll('button, input, select, textarea, summary, [tabindex="0"]')]
          .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
        const first = focusable[0], last = focusable.at(-1);
        if (!modal.contains(document.activeElement)) {
          event.preventDefault();
          (event.shiftKey ? last : first)?.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }, true);
    byId('image-skill-profile').addEventListener('change', () => {
      const profile = byId('image-skill-profile').value;
      modelOptions(profile);
      byId('image-skill-steps').value = defaults[profile].steps;
      dirty = true;
      controls();
      renderCards();
    });
    byId('image-skill-form').addEventListener('submit', event => {
      event.preventDefault();
      void perform(async () => {
        const response = await app.api(selected ? `${api}/${selected}` : api, {
          method: selected ? 'PUT' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draft()),
        });
        selected = response.skill.id;
        previousSelected = selected;
        dirty = false;
        await refresh();
        feedback(response.message);
      });
    });
    byId('image-skill-apply').addEventListener('click', () => activateSkill(selected));
    app.on('workspace:change', () => {
      if (app.runtime.navigation?.workspace !== 'ai') modalWanted = false;
      syncModalVisibility();
      if (app.runtime.navigation?.isVisible('image-skills')) void refresh();
    });
    edit(null);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
