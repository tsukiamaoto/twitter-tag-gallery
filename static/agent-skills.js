/* Project Markdown skills. Tool settings are deliberately unaware of this editor. */
(() => {
  'use strict';
  const app = window.GalleryApp, el = id => document.getElementById(id), api = '/api/agent-skills';
  let selected = null, opened = false, busy = false, loading = false, trigger = null;
  const visible = () => Boolean(app.runtime.navigation?.isVisible('image-skills'));
  const dirty = () => selected && el('agent-skill-content').value !== selected.content;
  function feedback(message, error = false) {
    const node = el('agent-skill-feedback');
    node.textContent = message;
    node.classList.toggle('error', error);
  }
  function controls() {
    const modal = el('agent-skill-modal');
    modal.hidden = !opened || !visible();
    modal.classList.toggle('open', !modal.hidden);
    el('agent-skill-content').disabled = busy;
    el('agent-skill-save').disabled = busy || !selected || !dirty();
    el('agent-skill-reload').disabled = busy || !selected;
    el('agent-skill-copy').disabled = busy || !selected;
    el('agent-skill-close').disabled = busy;
    el('agent-skill-draft').textContent = busy ? '處理中…' : dirty() ? '尚未儲存' : '已儲存';
    el('agent-skill-draft').dataset.state = dirty() ? 'dirty' : 'saved';
  }
  function show(document) {
    selected = { ...document, content: document.content.replace(/\r\n?/g, '\n') };
    el('agent-skill-title').textContent = document.name;
    el('agent-skill-path').textContent = document.path;
    el('agent-skill-content').value = selected.content;
    feedback(document.error || '修改只影響後續建立的任務；既有任務沿用原快照。', !document.valid);
    controls();
  }
  async function open(skill, button) {
    if (busy) return;
    if (selected?.id !== skill.id && dirty()) {
      el('agent-skills-feedback').textContent = '請先儲存目前的技能草稿，或在編輯視窗重新載入以放棄草稿。';
      opened = true; controls(); return;
    }
    trigger = button;
    if (selected?.id !== skill.id || !dirty()) {
      busy = true; controls();
      try { show(await app.api(`${api}/${encodeURIComponent(skill.id)}`)); }
      catch (error) { el('agent-skills-feedback').textContent = error.message; return; }
      finally { busy = false; controls(); }
    }
    opened = true;
    controls();
    if (visible()) el('agent-skill-content').focus();
  }
  function close() {
    if (busy) return;
    opened = false; controls();
    if (trigger?.isConnected) trigger.focus();
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    el('agent-skills-refresh').disabled = true;
    try {
      const data = await app.api(api), list = el('image-skill-cards');
      for (const card of list.querySelectorAll('.agent-skill-card')) card.remove();
      for (const skill of data.skills || []) {
        if (skill.kind === 'image-repair') continue; // rendered by the structured image Skill editor in this same list
        const card = document.createElement('article'); card.className = 'agent-skill-card';
        card.setAttribute('role', 'listitem'); card.dataset.agentSkillId = skill.id;
        const heading = document.createElement('h4'); heading.className = 'image-skill-list-card-title'; heading.textContent = skill.name;
        const status = document.createElement('span'); status.className = 'image-skill-badge';
        status.dataset.state = skill.valid ? 'available' : 'missing'; status.textContent = skill.valid ? '檔案可讀' : '檔案錯誤';
        const description = document.createElement('p'); description.className = 'image-skill-list-card-desc'; description.textContent = skill.error || skill.description;
        const model = document.createElement('p'); model.className = 'settings-note'; model.textContent = '執行模型：由外部 AI 客戶端設定';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn'; button.textContent = '檢視與編輯 SKILL.md';
        button.disabled = !skill.revision; button.addEventListener('click', () => void open(skill, button));
        card.append(heading, status, description, model, button); list.append(card);
      }
      el('agent-skills-feedback').textContent = data.migration_error || '';
    } catch (error) { el('agent-skills-feedback').textContent = error.message; }
    finally { loading = false; el('agent-skills-refresh').disabled = false; }
  }
  async function save() {
    if (!selected || busy || !dirty()) return;
    busy = true; controls();
    try {
      const document = await app.api(`${api}/${encodeURIComponent(selected.id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: el('agent-skill-content').value, revision: selected.revision }) });
      show(document); feedback('技能已儲存。新任務使用新內容；外部 AI 客戶端需重新載入。');
      await refresh();
      app.emit('skills:changed');
    } catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }
  async function reload() {
    if (busy || !selected) return;
    if (dirty() && !window.confirm('重新載入會放棄目前未儲存的修改。')) return;
    busy = true; controls();
    try { show(await app.api(`${api}/${encodeURIComponent(selected.id)}`)); }
    catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }
  function initialize() {
    if (!el('image-skill-cards') || el('agent-skill-modal')) return;
    const modal = document.createElement('div'); modal.id = 'agent-skill-modal'; modal.className = 'modal-backdrop'; modal.hidden = true;
    modal.innerHTML = `<section class="dialog-box agent-skill-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-skill-title">
      <header class="agent-skill-dialog-header"><div><h3 id="agent-skill-title"></h3></div><button type="button" class="btn" id="agent-skill-close" aria-label="關閉技能編輯視窗">關閉</button></header>
      <div class="agent-skill-dialog-body"><p id="agent-skill-path" class="settings-note"></p><label for="agent-skill-content">完整 SKILL.md（包含名稱與描述）</label><textarea id="agent-skill-content" class="search-input" spellcheck="false"></textarea><p id="agent-skill-feedback" class="settings-feedback" role="status"></p></div>
      <footer class="agent-skill-dialog-footer"><span id="agent-skill-draft" role="status"></span><div class="settings-actions"><button type="button" class="btn" id="agent-skill-copy">複製內容</button><button type="button" class="btn" id="agent-skill-reload">重新載入</button><button type="button" class="btn btn-primary" id="agent-skill-save">儲存修改</button></div></footer></section>`;
    document.body.append(modal);
    el('agent-skills-refresh').addEventListener('click', () => void refresh());
    el('agent-skill-content').addEventListener('input', controls);
    el('agent-skill-save').addEventListener('click', () => void save());
    el('agent-skill-reload').addEventListener('click', () => void reload());
    el('agent-skill-close').addEventListener('click', close);
    el('agent-skill-copy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(el('agent-skill-content').value); feedback(dirty() ? '已複製目前草稿，尚未儲存。' : '已複製完整技能內容。'); }
      catch (error) { feedback(error.message, true); }
    });
    modal.addEventListener('click', event => { if (event.target === modal) close(); });
    window.addEventListener('keydown', event => {
      if (modal.hidden) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); void save(); }
      if (event.key === 'Tab') {
        const items = [...modal.querySelectorAll('button, textarea')].filter(node => !node.disabled);
        const first = items[0], last = items.at(-1);
        if (!modal.contains(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
        else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }, true);
    app.on('workspace:change', () => { controls(); if (visible()) void refresh(); });
    app.on('skills:changed', () => void refresh());
    window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
    controls();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
