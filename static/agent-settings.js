/* Desktop agent tasks and editable MCP instructions. Uses the shared Settings API. */
(() => {
  'use strict';
  const app = window.GalleryApp, el = id => document.getElementById(id);
  let catalog = [], selected = null, modalOpen = false, lastTrigger = null, poll = null, currentJob = null, previewSequence = 0;
  const states = { queued: '等待 Antigravity', waiting: '等待 Antigravity', launching: '啟動對話中', running: '查證與改名中', launch_uncertain: '啟動結果待核對', succeeded: '完成', partial: '部分失敗', failed: '失敗', interrupted: '已中斷', cancelled: '已停止' };
  const active = status => ['queued', 'waiting', 'launching', 'running', 'launch_uncertain'].includes(status);
  function note(id, text, error = false) { const node = el(id); if (node) { node.textContent = text; node.classList.toggle('error', error); } }
  function button(text, action) { const node = document.createElement('button'); node.type = 'button'; node.className = 'btn'; node.textContent = text; node.addEventListener('click', action); return node; }
  function toolMeta(tool) {
    return `${tool.enabled ? '已啟用' : '已停用'} · ${tool.writes ? '寫入' : '唯讀'} · ${tool.workflow ? '模型查證 · Gemini 3.8 Flash' : '程式工具'}${tool.customized ? ' · 已自訂' : ''}`;
  }
  function dirty() {
    if (!selected) return false;
    const baseUsage = selected.usage_prompt ?? selected.description;
    return el('agent-usage-prompt').value !== baseUsage || Boolean(selected.task_prompt && el('agent-task-prompt').value !== selected.task_prompt.prompt);
  }
  function syncModal() {
    const modal = el('agent-tool-modal'), editor = el('agent-prompt-editor');
    if (!modal || !editor) return;
    const toolsVisible = app.runtime.navigation ? app.runtime.navigation.isVisible('tools') : !el('settings-tools')?.hidden;
    const visible = modalOpen && Boolean(toolsVisible);
    modal.hidden = !visible;
    modal.classList.toggle('open', visible);
    editor.hidden = !modalOpen;
  }
  function closeModal() {
    if (!modalOpen) return;
    modalOpen = false;
    syncModal();
    if (lastTrigger?.isConnected) lastTrigger.focus({ preventScroll: true });
  }
  function populateTool(tool, preserveDraft = false) {
    selected = tool;
    el('agent-prompt-name').textContent = `${tool.label} · ${tool.name}`;
    el('agent-prompt-meta').textContent = toolMeta(tool);
    if (!preserveDraft) {
      el('agent-usage-prompt').value = tool.usage_prompt ?? tool.description;
      el('agent-task-prompt').value = tool.task_prompt?.prompt || '';
      note('agent-prompt-feedback', `目前版本 ${tool.prompt_revision ?? 0}；儲存後套用新任務。MCP 客戶端請重新整理工具清單。`);
    }
    el('agent-task-prompt').disabled = !tool.workflow;
    el('agent-task-prompt').hidden = !tool.workflow;
    el('agent-task-label').textContent = tool.workflow ? `模型任務 Prompt · ${tool.workflow}（共用）` : '直接執行程式，無模型任務 Prompt';
  }
  function selectTool(tool, trigger = null) {
    if (trigger) lastTrigger = trigger;
    const preserveDraft = selected?.name === tool.name && dirty();
    populateTool(tool, preserveDraft);
    modalOpen = true;
    syncModal();
    el('agent-usage-prompt').focus({ preventScroll: true });
  }
  function renderCatalog() {
    const query = el('agent-tool-search').value.trim().toLowerCase(), list = el('agent-tool-catalog'); list.replaceChildren();
    const matching = catalog.filter(t => `${t.name} ${t.label} ${t.usage_prompt ?? t.description} ${t.description}`.toLowerCase().includes(query));
    note('agent-tool-count', `${matching.length} / ${catalog.length} 個工具（含停用項目）`);
    for (const tool of matching) {
      const row = button('', () => selectTool(tool, row)); row.classList.add('agent-tool-row'); row.dataset.tool = tool.name;
      const header = document.createElement('div'); header.className = 'agent-tool-row-header';
      const titleGroup = document.createElement('div'); titleGroup.className = 'agent-tool-row-title';
      const title = document.createElement('strong'); title.textContent = tool.label;
      const id = document.createElement('code'); id.textContent = tool.name;
      titleGroup.append(title, id);
      const info = document.createElement('span'); info.className = 'agent-tool-row-meta'; info.textContent = toolMeta(tool);
      header.append(titleGroup, info);
      const content = document.createElement('p'); content.className = 'agent-tool-row-content'; content.textContent = tool.usage_prompt ?? tool.description;
      row.append(header, content); list.append(row);
    }
  }
  async function loadCatalog(discard = false) {
    const data = await app.api('/api/settings/mcp/tools'); catalog = data.tools;
    renderCatalog();
    if (selected && catalog.some(t => t.name === selected.name)) {
      const fresh = catalog.find(t => t.name === selected.name);
      populateTool(fresh, !discard && dirty());
    }
  }
  async function savePrompt(reset = false) {
    if (!selected) return;
    const button = el('agent-prompt-save'); button.disabled = true;
    try {
      await app.api(`/api/settings/mcp/tools/${encodeURIComponent(selected.name)}/prompt`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ usage_prompt: el('agent-usage-prompt').value, task_prompt: selected.workflow ? el('agent-task-prompt').value : null, revision: selected.prompt_revision, reset }) });
      await loadCatalog(true); note('agent-prompt-feedback', '已儲存。新任務使用新版 Prompt；目前執行中的任務保持原版本。');
    } catch (error) { note('agent-prompt-feedback', error.message, true); }
    finally { button.disabled = false; }
  }
  function jobsVisible() {
    return !document.hidden && (!el('settings-tasks')?.hidden && el('schedule-modal')?.classList.contains('open') || currentJob && el('rename-modal')?.classList.contains('open'));
  }
  async function refreshJobs() {
    clearTimeout(poll);
    try {
      const { jobs, bridge } = await app.api('/api/agent/jobs');
      const status = !bridge.connected ? '等待 Antigravity 桌面橋接器連線' : bridge.model_verified ? 'Antigravity 已連線 · Gemini 3.8 Flash 已確認' : '已連線 · 模型待確認';
      note('agent-bridge-status', `${status}${bridge.error ? `：${bridge.error}` : ''}`);
      const list = el('agent-jobs'); if (!list) return; list.replaceChildren();
      for (const job of jobs) {
        const row = document.createElement('section'); row.className = 'settings-section';
        const title = document.createElement('p'); title.textContent = `${job.workflow === 'publication' ? '發布日期追查與改名' : '畫師身分查證'} · ${states[job.status] || job.status} · ${new Date(job.created_at * 1000).toLocaleString('zh-TW')}`;
        const detail = document.createElement('p'); detail.className = 'settings-note'; detail.textContent = `${job.context.folder || job.context.artist_name || ''} · 完成 ${job.counts?.done || 0} · 未知 ${job.counts?.unknown || 0} · 失敗 ${job.counts?.failed || 0}${job.conversation_id ? ` · Antigravity 對話 ${job.conversation_id}` : ''}${job.error ? ` · ${job.error}` : ''}`;
        const prompt = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre'); summary.textContent = `查看實際 Prompt · 版本 ${job.prompt.revision}`; pre.className = 'tool-output'; pre.textContent = job.prompt.rendered_prompt; prompt.append(summary, pre);
        row.append(title, detail, prompt);
        if (active(job.status)) row.append(button('停止這個工作', () => control(job.id, 'stop')));
        if (['failed', 'interrupted', 'cancelled', 'partial'].includes(job.status)) row.append(button('續跑未完成項目', () => control(job.id, 'resume')));
        list.append(row);
        if (currentJob === job.id) note('rn-summary-bar', `${states[job.status]} · ${status}${job.error ? ` · ${job.error}` : ''}`);
      }
      if (!jobs.length) list.textContent = '尚無 Antigravity 背景查證工作。';
    } catch (error) { note('agent-bridge-status', error.message, true); }
    if (jobsVisible()) poll = setTimeout(() => { if (jobsVisible()) void refreshJobs(); }, 3000);
  }
  async function control(id, action) {
    try { const job = await app.api(`/api/agent/jobs/${id}/${action}`, { method: 'POST' }); if (currentJob === id) currentJob = job.id; await refreshJobs(); }
    catch (error) { note('agent-bridge-status', error.message, true); }
  }
  async function preview() {
    const sequence = ++previewSequence;
    const folder = el('rn-folder').value;
    note('rn-summary-bar', '正在讀取已查證的發布日期…');
    try {
      const data = await app.api(`/api/rename/publication-preview?${new URLSearchParams({ folder })}`);
      if (sequence !== previewSequence || el('rn-mode').value !== 'publication') return;
      note('rn-summary-bar', `共 ${data.total.toLocaleString()} 張 · 已確認發布日期 ${data.verified.toLocaleString()} 張 · 待追查 ${data.pending.toLocaleString()} 張`);
      const box = el('rn-preview-box'); box.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement('p'); row.textContent = `${item.folder || '(根目錄)'} / ${item.filename} → ${item.published_at ? new Date(item.published_at * 1000).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' }) + ' · 已確認，將依日期重新編號' : '待追查；未確認前保留原名'}`; box.append(row);
      }
      el('btn-rn-exec').style.display = 'inline-flex';
    } catch (error) { if (sequence === previewSequence) note('rn-summary-bar', error.message, true); }
  }
  async function start() {
    const execute = el('btn-rn-exec'); execute.disabled = true;
    try {
      const job = await app.api('/api/agent/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workflow: 'publication', folder: el('rn-folder').value, pad_digits: Number(el('rn-pad').value) }) });
      currentJob = job.id; note('rn-summary-bar', '工作已排入背景佇列，可關閉此視窗；進度請至設定 → 背景任務與復原查看。');
      await refreshJobs();
    } catch (error) { note('rn-summary-bar', error.message, true); }
    finally { execute.disabled = false; }
  }
  function switchMode() {
    const publication = el('rn-mode').value === 'publication';
    for (const id of ['rn-date', 'rn-artist']) el(id).closest('label').hidden = publication;
    el('btn-rn-exec').textContent = publication ? '開始背景追查與改名' : '確認執行手動改名';
    void window.loadRenamePreview();
  }
  window.GalleryAgent = { preview, start, switchMode };
  function initialize() {
    const panel = el('settings-mcp'), toolsPanel = el('settings-tools');
    if (!panel || !toolsPanel) return;
    const section = document.createElement('section'); section.className = 'agent-management';
    section.innerHTML = `<div class="agent-management-header"><h3>工具列表與 Prompt</h3><p class="settings-note">查看全部圖庫 MCP 工具內容，點擊工具可彈出視窗檢視與編輯工具使用說明及共用模型任務 Prompt。</p></div><div class="agent-tool-toolbar"><input id="agent-tool-search" class="search-input" placeholder="搜尋工具名稱、ID 或用途" aria-label="搜尋工具"><p id="agent-tool-count" class="settings-note"></p></div><div id="agent-tool-catalog" class="agent-tool-catalog"></div>`;
    const permissions = toolsPanel.querySelector('details.settings-section');
    toolsPanel.prepend(section);
    if (permissions) section.querySelector('.agent-management-header').after(permissions);
    const modal = document.createElement('div');
    modal.id = 'agent-tool-modal';
    modal.className = 'modal-backdrop';
    modal.hidden = true;
    modal.innerHTML = `<div id="agent-prompt-editor" class="dialog-box agent-tool-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-prompt-name" tabindex="-1" hidden><div class="agent-tool-dialog-header"><div class="agent-tool-dialog-heading"><p class="agent-tool-dialog-eyebrow">Tool 設定 · 工具列表與 Prompt</p><h3 id="agent-prompt-name"></h3><p id="agent-prompt-meta" class="settings-note"></p></div><button id="agent-prompt-close" type="button" class="btn" aria-label="關閉工具視窗">✕</button></div><div class="agent-tool-dialog-body"><label>工具使用說明／Prompt<textarea id="agent-usage-prompt" class="search-input" rows="6" maxlength="16000"></textarea></label><label id="agent-task-field"><span id="agent-task-label"></span><textarea id="agent-task-prompt" class="search-input" rows="10" maxlength="24000"></textarea></label><p id="agent-prompt-feedback" class="settings-feedback" role="status"></p></div><div class="agent-tool-dialog-footer"><div class="settings-actions"><button id="agent-prompt-save" type="button" class="btn btn-primary">儲存 Prompt</button><button id="agent-prompt-reset" type="button" class="btn">恢復預設並儲存</button><button id="agent-prompt-reload" type="button" class="btn">放棄編輯並重新載入</button></div><button id="agent-prompt-cancel" type="button" class="btn">關閉</button></div></div>`;
    document.body.append(modal);
    const tasks = document.createElement('section'); tasks.className = 'settings-section'; tasks.innerHTML = '<h3>Antigravity 桌面背景查證</h3><p id="agent-bridge-status" role="status"></p><p class="settings-note">由桌面版 Sidecar 執行，請保持 Antigravity 在背景運作。圖庫頁面可關閉。所有模型查證固定使用已確認的 Gemini 3.8 Flash。</p><div id="agent-jobs"></div>';
    panel.append(tasks);
    el('agent-tool-search').addEventListener('input', renderCatalog);
    el('agent-prompt-save').addEventListener('click', () => savePrompt());
    el('agent-prompt-reset').addEventListener('click', () => savePrompt(true));
    el('agent-prompt-reload').addEventListener('click', () => loadCatalog(true).catch(error => note('agent-prompt-feedback', error.message, true)));
    el('agent-prompt-close').addEventListener('click', closeModal);
    el('agent-prompt-cancel').addEventListener('click', closeModal);
    modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
    window.addEventListener('keydown', event => {
      if (modal.hidden) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeModal();
        return;
      }
      if (event.key === 'Tab') {
        const focusable = [...modal.querySelectorAll('button, input, select, textarea, [tabindex="0"]')]
          .filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
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
    app.use('switchSettingsTab', (next, tab) => { const result = next(tab); clearTimeout(poll); if (tab === 'tasks') void refreshJobs(); return result; });
    app.on('workspace:change', () => {
      syncModal();
      if (app.runtime.navigation.isVisible('tools')) void loadCatalog().catch(error => note('agent-prompt-feedback', error.message, true));
      if (!jobsVisible()) clearTimeout(poll);
    });
    app.on('tools:permissions', () => { void loadCatalog().catch(error => note('agent-prompt-feedback', error.message, true)); });
    window.closeAgentToolModal = closeModal;
    app.use('closeScheduleModal', (next, ...args) => { closeModal(); const result = next(...args); if (!jobsVisible()) clearTimeout(poll); return result; });
    document.addEventListener('visibilitychange', () => { if (jobsVisible()) void refreshJobs(); else clearTimeout(poll); });
    for (const id of ['rn-date', 'rn-artist']) el(id).closest('label').hidden = true;
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
