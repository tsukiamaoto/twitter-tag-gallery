/* Settings controllers use shared API/runtime state; model authentication belongs to the MCP client. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id);
  if (app.runtime.integrationsInstalled) return;
  app.runtime.integrationsInstalled = true;
  let configuration = null, jobsSignature = '', loading = null;
  let tools = [], toolsLoading = null, selectedRun = null, toolPoll = null, runLoading = false;
  const labels = { mcp: 'MCP 管理', tasks: '背景任務與復原' };
  function feedback(id, text, error = false) {
    byId(id).textContent = text; byId(id).classList.toggle('error', error);
  }
  function addPanel(name, content) {
    const tab = document.createElement('button'); tab.className = 'btn settings-tab'; tab.id = `settings-tab-${name}`;
    tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', 'false'); tab.setAttribute('aria-controls', `settings-${name}`);
    tab.tabIndex = -1; tab.textContent = labels[name]; tab.addEventListener('click', () => window.switchSettingsTab(name));
    document.querySelector('.settings-tabs').append(tab);
    const panel = document.createElement('section'); panel.id = `settings-${name}`; panel.className = 'settings-panel'; panel.hidden = true;
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id); panel.innerHTML = content;
    document.querySelector('.settings-content').append(panel);
  }
  function updatePermissions() {
    const enabled = byId('mcp-enabled').checked, readOnly = byId('mcp-readonly').checked;
    for (const input of byId('mcp-tool-list').querySelectorAll('input')) input.disabled = !enabled || readOnly && input.dataset.writes === 'true';
  }
  async function loadSettings(force = false) {
    if (loading) return loading;
    if (configuration && !force) return configuration;
    for (const control of byId('settings-mcp').querySelectorAll('input, button, select')) control.disabled = true;
    loading = app.api('/api/settings/integrations').then(data => {
      configuration = data;
      byId('mcp-enabled').checked = data.mcp_enabled; byId('mcp-readonly').checked = data.mcp_read_only;
      const list = byId('mcp-tool-list'); list.replaceChildren();
      for (const tool of data.tools) {
        const label = document.createElement('label'); label.className = 'settings-check';
        const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.name = tool.name; input.dataset.writes = String(tool.writes);
        input.checked = !data.disabled_tools.includes(tool.name);
        const text = document.createElement('span'); text.textContent = `${tool.label} · ${tool.writes ? '可修改圖庫' : '唯讀'}`;
        label.append(input, text); list.append(label);
      }
      updatePermissions();
      byId('mcp-session-status').textContent = data.sessions?.length ? `最近工具存取：${new Date(data.sessions[0].last_seen * 1000).toLocaleString('zh-TW')}（PID ${data.sessions[0].pid}）` : '尚未收到 MCP 客戶端的工具存取。';
      if (data.config_error) feedback('mcp-feedback', data.config_error, true);
    }).catch(error => { feedback('mcp-feedback', `設定讀取失敗：${error.message}`, true); }).finally(() => {
      loading = null;
      for (const control of byId('settings-mcp').querySelectorAll('input, button, select')) control.disabled = false;
      updatePermissions();
      renderTool();
    });
    return loading;
  }
  async function action(button, statusId, operation) {
    if (button.disabled) return;
    button.disabled = true; feedback(statusId, '處理中…');
    try { const result = await operation(); feedback(statusId, result?.message || '已儲存。', result?.ok === false); return result; }
    catch (error) { feedback(statusId, error.message, true); }
    finally { button.disabled = false; }
  }
  async function save(button) {
    const value = { mcp_enabled: byId('mcp-enabled').checked, mcp_read_only: byId('mcp-readonly').checked,
      disabled_tools: [...byId('mcp-tool-list').querySelectorAll('input')].filter(input => !input.checked).map(input => input.dataset.name) };
    const result = await action(button, 'mcp-feedback', () => app.api('/api/settings/integrations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }));
    if (result) {
      configuration = { ...configuration, ...result };
      feedback('mcp-feedback', '權限已儲存並立即套用。客戶端請重新整理 MCP 工具清單。');
      if (tools.length) await loadTools(true);
    }
  }
  function selectedTool() { return tools.find(tool => tool.name === byId('mcp-tool-select').value); }
  function renderTool() {
    const tool = selectedTool();
    if (!tool) { byId('mcp-tool-run').disabled = true; return; }
    byId('mcp-tool-description').textContent = tool.description;
    byId('mcp-tool-effect').textContent = `${tool.exclusive === false && tool.writes ? '任務控制：要求停止目前作業。' : tool.writes ? '會修改圖庫，依參數可能搬移檔案。' : '唯讀查詢。'}${tool.network ? '可能連線外部服務或下載模型。' : ''}${tool.background ? 'MCP 呼叫會先回傳任務 ID。' : ''}${tool.enabled ? '' : '此工具已被儲存的權限設定停用。'}`;
    byId('mcp-tool-schema').textContent = JSON.stringify(tool.inputSchema, null, 2);
    byId('mcp-tool-run').textContent = tool.exclusive === false && tool.writes ? '送出停止要求' : tool.writes ? '執行並修改圖庫' : '執行唯讀查詢';
    byId('mcp-tool-run').disabled = !tool.enabled || selectedRun?.status === 'running';
  }
  async function loadTools(force = false) {
    if (toolsLoading) return toolsLoading;
    if (tools.length && !force) { renderTool(); return; }
    byId('mcp-tool-run').disabled = true;
    toolsLoading = app.api('/api/settings/mcp/tools').then(data => {
      const select = byId('mcp-tool-select'), previous = select.value;
      tools = data.tools; select.replaceChildren();
      for (const tool of tools) {
        const option = document.createElement('option'); option.value = tool.name;
        option.textContent = `${tool.label} · ${tool.writes ? '寫入' : '唯讀'}${tool.enabled ? '' : '（已停用）'}`; select.append(option);
      }
      if (tools.some(tool => tool.name === previous)) select.value = previous;
      if (!previous) byId('mcp-tool-arguments').value = JSON.stringify(selectedTool()?.example || {}, null, 2);
      renderTool();
    }).catch(error => feedback('mcp-tool-feedback', error.message, true)).finally(() => { toolsLoading = null; });
    return toolsLoading;
  }
  const toolStates = { running: '執行中', succeeded: '完成', failed: '失敗', cancelled: '已停止', interrupted: '程序中止' };
  async function loadToolHistory() {
    const { runs } = await app.api('/api/settings/mcp/runs');
    const list = byId('mcp-tool-history'); list.replaceChildren();
    for (const run of runs) {
      const button = document.createElement('button'); button.className = 'btn tool-history-row';
      const label = tools.find(tool => tool.name === run.name)?.label || run.name;
      button.textContent = `${label} · ${toolStates[run.status] || run.status} · ${new Date(run.started_at * 1000).toLocaleString('zh-TW')}${run.duration_ms == null ? '' : ` · ${run.duration_ms} ms`}`;
      button.disabled = selectedRun?.status === 'running' && selectedRun.id !== run.id;
      button.addEventListener('click', () => {
        if (selectedRun?.status === 'running' && selectedRun.id !== run.id) return;
        selectedRun = { id: run.id, status: run.status }; void refreshToolRun();
      }); list.append(button);
    }
    if (!runs.length) list.textContent = '尚無工具執行紀錄。';
  }
  function toolVisible() {
    return byId('mcp-tool-workbench').open && !byId('settings-mcp').hidden && byId('schedule-modal').classList.contains('open');
  }
  function scheduleToolPoll() {
    clearTimeout(toolPoll);
    if (selectedRun?.status !== 'running' || document.hidden || !toolVisible()) return;
    toolPoll = setTimeout(() => {
      if (!document.hidden && toolVisible()) void refreshToolRun();
    }, 1000);
  }
  async function refreshToolRun() {
    if (!selectedRun || runLoading) return;
    runLoading = true;
    try {
      const { run } = await app.api(`/api/settings/mcp/runs/${encodeURIComponent(selectedRun.id)}`);
      selectedRun = run;
      feedback('mcp-tool-feedback', `${toolStates[run.status] || run.status}${run.duration_ms == null ? '' : ` · ${run.duration_ms} ms`}${run.error ? `：${run.error}` : ''}${run.truncated ? '（結果過長，僅保留預覽）' : ''}`, ['failed', 'interrupted'].includes(run.status));
      byId('mcp-tool-result').textContent = run.result == null ? '' : JSON.stringify(run.result, null, 2);
      byId('mcp-tool-stop').hidden = !(run.status === 'running' && run.job_id);
      byId('mcp-tool-run').disabled = run.status === 'running' || !selectedTool()?.enabled;
      if (run.status !== 'running') await loadToolHistory();
    } catch (error) { feedback('mcp-tool-feedback', error.message, true); }
    finally { runLoading = false; scheduleToolPoll(); }
  }
  async function runTool() {
    const tool = selectedTool();
    if (!tool?.enabled || selectedRun?.status === 'running') return;
    let argumentsValue;
    try {
      argumentsValue = JSON.parse(byId('mcp-tool-arguments').value);
      if (!argumentsValue || typeof argumentsValue !== 'object' || Array.isArray(argumentsValue)) throw new Error('object');
    } catch { feedback('mcp-tool-feedback', '參數必須為有效的 JSON 物件。', true); return; }
    byId('mcp-tool-run').disabled = true; byId('mcp-tool-result').textContent = '';
    feedback('mcp-tool-feedback', '正在啟動工具…');
    try {
      selectedRun = await app.api(`/api/settings/mcp/tools/${encodeURIComponent(tool.name)}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ arguments: argumentsValue }) });
      await refreshToolRun();
    } catch (error) { feedback('mcp-tool-feedback', error.message, true); renderTool(); }
  }
  function renderJobs(runtime) {
    const jobs = runtime.jobs || [], active = runtime.active_job;
    const health=runtime.library_health;
    if (health) byId('tasks-library-health').textContent=`保留待確認遺失 ${health.missing} 張 · 檔案指紋 ${health.hashed} / ${health.total} · 待重試處理 ${health.failed_stages} 項`;
    byId('tasks-current').textContent = active?.id ? `${active.name} · ${active.stage || '處理中'} · ${active.processed_count || 0} / ${active.total_target || '待確認'}` : '目前沒有背景作業';
    const signature = JSON.stringify(jobs.map(job => [job.id, job.status, job.stage, job.processed_count, job.last_error]));
    if (signature === jobsSignature) return; jobsSignature = signature;
    const list = byId('tasks-history'); list.replaceChildren();
    const states = { running: '處理中', succeeded: '完成', failed: '失敗', interrupted: '程序中止', cancelled: '已停止' };
    for (const job of jobs) {
      const row = document.createElement('div'); row.className = 'settings-section';
      const title = document.createElement('p'); title.textContent = `${job.name} · ${states[job.status] || job.status}`; row.append(title);
      if (job.last_error) { const error = document.createElement('p'); error.className = 'settings-note'; error.textContent = job.last_error; row.append(error); }
      if (['failed', 'interrupted', 'cancelled'].includes(job.status) && job.recipe) {
        const button = document.createElement('button'); button.className = 'btn'; button.textContent = '重試未完成項目';
        button.disabled = Boolean(active?.id); button.addEventListener('click', () => action(button, 'tasks-feedback', async () => {
          await app.api(`/api/jobs/${encodeURIComponent(job.id)}/resume`, { method: 'POST' }); await window.checkAiStatus(); return { message: '已重新啟動未完成項目。' };
        })); row.append(button);
      }
      list.append(row);
    }
    if (!jobs.length) list.textContent = '尚無任務紀錄。';
  }
  function initialize() {
    if (!byId('schedule-modal')) return;
    addPanel('mcp', `<div><h3>MCP 管理</h3><p class="settings-note">由 Antigravity 或 Gemini CLI 呼叫圖庫工具。登入與模型選擇在客戶端管理；本頁管理圖庫權限與連線設定。</p></div>
      <div class="settings-section"><label class="settings-check"><input id="mcp-enabled" type="checkbox"><span>啟用圖庫 MCP</span></label><label class="settings-check"><input id="mcp-readonly" type="checkbox"><span>唯讀模式：停用所有會修改圖庫的工具</span></label><p id="mcp-session-status" class="settings-note"></p></div>
      <details class="settings-section"><summary>工具權限</summary><div id="mcp-tool-list"></div></details>
      <label>連線客戶端<select id="mcp-client-target" class="search-input"><option value="antigravity">Antigravity</option><option value="gemini_cli">Gemini CLI</option></select></label><div class="settings-actions"><button id="mcp-save" class="btn btn-primary">儲存 MCP 設定</button><button id="mcp-test" class="btn">測試本機連線</button><button id="mcp-export" class="btn">產生連線設定</button></div>
      <p class="settings-note">設定儲存於圖庫；停用會阻擋現有連線的工具呼叫。診斷只測試握手及工具清單。</p>
      <textarea id="mcp-client-config" class="search-input" rows="10" readonly aria-label="MCP 客戶端連線設定" hidden></textarea><p id="mcp-export-note" class="settings-note" hidden></p><button id="mcp-copy" class="btn" hidden>複製設定</button><p id="mcp-feedback" class="settings-feedback" role="status"></p>
      <details id="mcp-tool-workbench" class="settings-section"><summary>工具操作與執行紀錄</summary><div class="tool-workbench">
        <label>選擇工具<select id="mcp-tool-select" class="search-input" aria-label="選擇 MCP 工具"></select></label>
        <p id="mcp-tool-description" class="settings-note"></p><p id="mcp-tool-effect" class="settings-note"></p>
        <details><summary>參數格式與限制</summary><pre id="mcp-tool-schema" class="tool-output"></pre></details>
        <label>參數（JSON）<textarea id="mcp-tool-arguments" class="search-input" rows="8" spellcheck="false" aria-label="工具 JSON 參數">{}</textarea></label>
        <div class="settings-actions"><button id="mcp-tool-example" class="btn">填入參數範例</button><button id="mcp-tool-run" class="btn btn-primary" disabled>執行工具</button><button id="mcp-tool-stop" class="btn" hidden>要求停止</button></div>
        <p class="settings-note">執行使用已儲存的權限。範例需自行核對圖片 ID 與標籤；執行按鈕會實際呼叫工具。紀錄保留最近 200 次、每次最多 128 KiB 結果，不儲存輸入參數。</p>
        <p id="mcp-tool-feedback" class="settings-feedback" role="status"></p><pre id="mcp-tool-result" class="tool-output" aria-label="工具執行結果"></pre>
        <div class="settings-actions"><button id="mcp-tool-refresh" class="btn">重新整理執行紀錄</button></div><div id="mcp-tool-history" class="tool-history"></div>
      </div></details>`);
    addPanel('tasks', `<div><h3>背景任務與復原</h3><p id="tasks-current" role="status">讀取中…</p><p id="tasks-library-health" class="settings-note"></p><p class="settings-note">任務狀態跨 Web、Windows 排程與 MCP 共用。重試會依處理狀態選擇未完成項目。</p></div>
      <div class="settings-actions"><button id="tasks-recover" class="btn">檢查並復原中斷的檔案操作</button><button id="tasks-backup" class="btn">備份資料庫</button></div><p id="tasks-feedback" class="settings-feedback" role="status"></p><div id="tasks-history"></div>`);
    byId('mcp-enabled').addEventListener('change', updatePermissions); byId('mcp-readonly').addEventListener('change', updatePermissions);
    byId('mcp-save').addEventListener('click', event => save(event.currentTarget));
    byId('mcp-test').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', () => app.api('/api/settings/mcp/test', { method: 'POST' })));
    byId('mcp-tool-workbench').addEventListener('toggle', () => {
      if (byId('mcp-tool-workbench').open) void loadTools().then(loadToolHistory).then(() => refreshToolRun()).catch(error => feedback('mcp-tool-feedback', error.message, true));
      else clearTimeout(toolPoll);
    });
    byId('mcp-tool-select').addEventListener('change', renderTool);
    byId('mcp-tool-example').addEventListener('click', () => { byId('mcp-tool-arguments').value = JSON.stringify(selectedTool()?.example || {}, null, 2); });
    byId('mcp-tool-run').addEventListener('click', runTool);
    byId('mcp-tool-refresh').addEventListener('click', event => action(event.currentTarget, 'mcp-tool-feedback', async () => { await loadTools(true); await loadToolHistory(); await refreshToolRun(); return { message: '已重新整理工具與執行紀錄。' }; }));
    byId('mcp-tool-stop').addEventListener('click', event => action(event.currentTarget, 'mcp-tool-feedback', () => app.api(`/api/jobs/${encodeURIComponent(selectedRun.job_id)}/stop`, { method: 'POST' })));
    byId('mcp-export').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      const client = byId('mcp-client-target').value;
      const value = await app.api(`/api/settings/mcp/client_config?client=${encodeURIComponent(client)}`); byId('mcp-client-config').value = JSON.stringify(value, null, 2);
      for (const id of ['mcp-client-config', 'mcp-copy', 'mcp-export-note']) byId(id).hidden = false;
      byId('mcp-export-note').textContent = client === 'antigravity' ? '在 Antigravity 的 MCP 管理開啟設定，將內容合併至 mcp_config.json 的 mcpServers 欄位；保留既有伺服器。' : '將內容合併至 Gemini CLI 的 settings.json 的 mcpServers 欄位；保留既有伺服器。';
      return { message: `已產生 ${client === 'antigravity' ? 'Antigravity' : 'Gemini CLI'} 連線設定。` };
    }));
    byId('mcp-client-target').addEventListener('change', () => { for (const id of ['mcp-client-config', 'mcp-copy', 'mcp-export-note']) byId(id).hidden = true; });
    byId('mcp-copy').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      await navigator.clipboard.writeText(byId('mcp-client-config').value); return { message: '已複製。' };
    }));
    byId('tasks-recover').addEventListener('click', event => action(event.currentTarget, 'tasks-feedback', async () => {
      const result = await app.api('/api/maintenance/recover', { method: 'POST' });
      if (result.errors?.length) throw new Error(result.errors.map(item => item.error).join('；'));
      return { message: `復原檢查完成，處理 ${result.recovered.length} 個中斷批次。` };
    }));
    byId('tasks-backup').addEventListener('click', event => action(event.currentTarget, 'tasks-feedback', () => app.api('/api/maintenance/backup', { method: 'POST' })));
    app.use('switchSettingsTab', (next, section) => {
      const result = next(section);
      if (section === 'mcp') { void loadSettings(); byId('settings-footer-note').textContent = '修改後請按此區的儲存按鈕。'; }
      if (section === 'mcp' && byId('mcp-tool-workbench').open) void loadTools().then(loadToolHistory).then(() => refreshToolRun()).catch(error => feedback('mcp-tool-feedback', error.message, true));
      if (section !== 'mcp') clearTimeout(toolPoll);
      if (section === 'tasks') { void app.refreshRuntime().catch(error => feedback('tasks-feedback', error.message, true)); byId('settings-footer-note').textContent = '檔案復原與備份會取得共用作業鎖。'; }
      return result;
    });
    app.on('runtime', renderJobs);
    app.on('runtime:error', message => { byId('tasks-current').textContent = `任務狀態暫時無法讀取：${message}`; });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && toolVisible() && selectedRun?.status === 'running') void refreshToolRun();
      else clearTimeout(toolPoll);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
