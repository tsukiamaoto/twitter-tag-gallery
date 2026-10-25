/* Settings controllers use shared API/runtime state; model authentication belongs to the MCP client. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id);
  if (app.runtime.integrationsInstalled) return;
  app.runtime.integrationsInstalled = true;
  let configuration = null, servers = [], selectedId = 'gallery', isNew = false;
  let dirty = false, busy = false, modalWanted = false, returnFocus = null, newServerId = '';
  let jobsSignature = '', loading = null;
  const labels = { mcp: 'MCP 管理', tools: 'Tool 設定', tasks: '背景任務與復原', artists: '畫師身分', dedup: '清除重複圖片' };

  function feedback(id, text, error = false) {
    const node = byId(id);
    if (!node) return;
    node.textContent = text;
    node.classList.toggle('error', error);
  }

  function mcpFeedback(text, error = false) {
    feedback('mcp-feedback', text, error);
    feedback('mcp-page-feedback', text, error);
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

  function normalizeServers(data) {
    if (Array.isArray(data?.servers) && data.servers.length) {
      return data.servers.map(server => ({
        ...server,
        args: [...(server.args || [])],
        env: { ...(server.env || {}) },
        headers: { ...(server.headers || {}) },
        disabled_tools: [...(server.disabled_tools || [])],
        custom_config: { ...(server.custom_config || {}) },
      }));
    }
    const enabled = Boolean(data?.mcp_enabled ?? true);
    const readOnly = Boolean(data?.mcp_read_only ?? false);
    const disabledTools = [...(data?.disabled_tools || [])];
    return [
      {
        id: 'gallery',
        server_name: 'gallery',
        name: 'Antigravity 圖庫 MCP',
        category: 'Antigravity 內建 MCP',
        description: '提供 Antigravity 對話直接呼叫的 37 個圖庫工具（搜尋、標籤相關性、語意搜尋、畫師校正、批次歸檔與重複清理）。',
        builtin: true,
        transport: 'stdio',
        command: 'python',
        args: ['mcp_server.py'],
        cwd: '',
        env: { PYTHONIOENCODING: 'utf-8' },
        url: '',
        headers: {},
        enabled,
        read_only: readOnly,
        disabled_tools: disabledTools,
        custom_config: { client: 'antigravity', role: 'interactive_gallery' },
      },
      {
        id: 'gallerypub',
        server_name: 'gallerypub_sidecar',
        name: 'Antigravity 背景查證 MCP (Sidecar)',
        category: 'Antigravity 內建 MCP',
        description: '桌面背景佇列專用的查證 MCP 通道，固定搭配 Gemini 3.8 Flash 執行圖片發布日期追查與跨平台畫師身分查證。',
        builtin: true,
        transport: 'stdio',
        command: 'python',
        args: ['mcp_server.py'],
        cwd: '',
        env: { PYTHONIOENCODING: 'utf-8' },
        url: '',
        headers: {},
        enabled,
        read_only: readOnly,
        disabled_tools: disabledTools,
        custom_config: { client: 'antigravity', role: 'background_sidecar', fixed_model: 'Gemini 3.8 Flash' },
      },
    ];
  }

  function formatKeyValue(obj, separator = '=') {
    if (!obj || typeof obj !== 'object') return '';
    return Object.entries(obj)
      .filter(([key]) => String(key).trim())
      .map(([key, value]) => `${key}${separator}${typeof value === 'object' ? JSON.stringify(value) : value}`)
      .join('\n');
  }

  function parseKeyValue(text, primarySeparator = '=') {
    const result = {};
    for (const rawLine of String(text || '').split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      let idx = line.indexOf(primarySeparator);
      if (idx < 0 && primarySeparator !== '=') idx = line.indexOf('=');
      if (idx < 0 && primarySeparator !== ':') idx = line.indexOf(':');
      if (idx <= 0) {
        result[line] = '';
        continue;
      }
      const key = line.slice(0, idx).trim();
      const value = line.slice(idx + 1).trim();
      if (key) result[key] = value;
    }
    return result;
  }

  function parseLines(text) {
    return String(text || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  }

  function isMcpSectionVisible() {
    if (app.runtime.navigation) return app.runtime.navigation.isVisible('mcp');
    return Boolean(byId('settings-mcp') && !byId('settings-mcp').hidden && byId('schedule-modal')?.classList.contains('open'));
  }

  function syncModalVisibility() {
    const modal = byId('mcp-modal');
    if (!modal) return;
    const visible = modalWanted && isMcpSectionVisible();
    modal.hidden = !visible;
    modal.classList.toggle('open', visible);
  }

  function updatePermissions() {
    const modalToolList = byId('mcp-modal-tool-list');
    if (modalToolList && byId('mcp-enabled') && byId('mcp-readonly')) {
      const modalEnabled = byId('mcp-enabled').checked;
      const modalReadOnly = byId('mcp-readonly').checked;
      let allowedCount = 0, totalCount = 0;
      for (const input of modalToolList.querySelectorAll('input')) {
        totalCount += 1;
        const lockedByReadOnly = modalReadOnly && input.dataset.writes === 'true';
        input.disabled = busy || !modalEnabled || lockedByReadOnly;
        if (modalEnabled && !lockedByReadOnly && input.checked) allowedCount += 1;
      }
      const summary = byId('mcp-modal-tools-summary');
      if (summary) {
        const current = isNew ? null : servers.find(item => item.id === selectedId);
        if (current?.builtin) {
          summary.textContent = `${allowedCount} / ${totalCount} 個工具可呼叫`;
        } else {
          const disabledCount = parseLines(byId('mcp-server-disabled-tools')?.value).length;
          summary.textContent = disabledCount ? `停用 ${disabledCount} 個工具` : '全部工具允許呼叫';
        }
      }
    }
  }

  function syncModalControls() {
    const modal = byId('mcp-modal');
    if (!modal) return;
    const current = isNew ? null : servers.find(item => item.id === selectedId);
    const builtin = Boolean(current?.builtin);
    const transport = (byId('mcp-server-transport').value || 'stdio').toLowerCase();
    const stateNode = byId('mcp-draft-state');
    stateNode.textContent = busy ? '處理中…' : dirty ? '尚未儲存' : isNew ? '尚未建立' : '已儲存';
    stateNode.dataset.state = dirty ? 'dirty' : 'saved';

    byId('mcp-modal-title').textContent = isNew ? '新增 MCP 服務' : current?.name || 'MCP 設定';
    byId('mcp-modal-eyebrow').textContent = builtin ? '內建服務 · MCP 設定' : '自訂服務 · MCP 設定';
    byId('mcp-modal-description').textContent = builtin ? current.description || '調整此服務的存取權限。' : isNew ? '設定服務名稱與連線方式，即可加入 MCP。' : '調整此服務的連線與存取權限。';
    byId('mcp-name-field').hidden = builtin;
    byId('mcp-custom-fields').hidden = builtin;
    byId('mcp-transport-field').hidden = builtin;
    byId('mcp-connection-note').hidden = !builtin;
    byId('mcp-readonly-note').textContent = builtin ? '只允許查詢，不允許修改圖庫。' : '限制為唯讀工具，實際支援依服務而定。';
    byId('mcp-advanced-summary').textContent = builtin ? '啟動路徑、環境變數' : '環境與擴充參數';
    const connection = byId('mcp-connection-settings');
    const destination = byId(builtin ? 'mcp-advanced-connection' : 'mcp-custom-connection');
    if (connection.parentElement !== destination) destination.append(connection);
    byId('mcp-custom-connection').hidden = builtin;
    byId('mcp-server-transport').disabled = busy || builtin;
    byId('mcp-stdio-fields').hidden = transport !== 'stdio';
    byId('mcp-http-fields').hidden = transport === 'stdio';
    byId('mcp-stdio-options').hidden = transport !== 'stdio';
    byId('mcp-http-options').hidden = transport === 'stdio';
    byId('mcp-sidecar-fields').hidden = selectedId !== 'gallerypub' || isNew;
    byId('mcp-modal-tools-builtin').hidden = !builtin;
    byId('mcp-modal-tools-custom').hidden = builtin;
    byId('mcp-delete').hidden = isNew || builtin;
    byId('mcp-delete').disabled = busy;
    byId('mcp-test').disabled = busy || isNew;
    byId('mcp-test').hidden = isNew;
    byId('mcp-modal-client-details').hidden = isNew;
    byId('mcp-export').disabled = busy || isNew;
    byId('mcp-sync-client').disabled = busy || isNew;
    byId('mcp-save').disabled = busy;
    byId('mcp-save').textContent = isNew ? '新增服務' : '儲存變更';
    updatePermissions();
  }

  function populateModal(server, preserveDraft = false) {
    isNew = !server;
    selectedId = server?.id || '';
    if (!preserveDraft) {
      newServerId = server ? '' : `mcp-${[...crypto.getRandomValues(new Uint32Array(2))].map(value => value.toString(16).padStart(8, '0')).join('')}`;
      const value = server || {
        id: '',
        name: '',
        category: '自訂擴充 MCP',
        description: '',
        builtin: false,
        transport: 'stdio',
        command: '',
        args: [],
        cwd: '',
        env: {},
        url: '',
        headers: {},
        enabled: true,
        read_only: false,
        disabled_tools: [],
        custom_config: {},
      };
      byId('mcp-enabled').checked = Boolean(value.enabled);
      byId('mcp-readonly').checked = Boolean(value.read_only);
      byId('mcp-server-name').value = value.name || '';
      byId('mcp-server-description').value = value.description || '';
      byId('mcp-server-transport').value = (value.transport || 'stdio').toLowerCase();
      byId('mcp-server-command').value = value.command || '';
      byId('mcp-server-args').value = (value.args || []).join('\n');
      byId('mcp-server-cwd').value = value.cwd || '';
      byId('mcp-server-env').value = formatKeyValue(value.env, '=');
      byId('mcp-server-url').value = value.url || '';
      byId('mcp-server-headers').value = formatKeyValue(value.headers, ': ');
      byId('mcp-server-disabled-tools').value = (value.disabled_tools || []).join('\n');
      byId('mcp-server-custom-config').value = formatKeyValue(value.custom_config, '=');

      const disabledSet = new Set(value.disabled_tools || configuration?.disabled_tools || []);
      for (const input of byId('mcp-modal-tool-list').querySelectorAll('input')) {
        input.checked = !disabledSet.has(input.dataset.toolName);
      }
      for (const id of ['mcp-client-config', 'mcp-copy', 'mcp-export-note']) byId(id).hidden = true;
      for (const id of ['mcp-modal-tools-details', 'mcp-modal-advanced-details', 'mcp-modal-client-details']) byId(id).open = false;
      byId('mcp-modal-body').scrollTop = 0;
      dirty = false;
    }
    syncModalControls();
    renderCards();
  }

  function openModal(serverOrId, trigger = document.activeElement) {
    if (trigger && trigger !== document.body) returnFocus = trigger;
    let target = null;
    if (typeof serverOrId === 'string') {
      target = servers.find(item => item.id === serverOrId) || null;
    } else if (serverOrId && typeof serverOrId === 'object') {
      target = serverOrId;
    }
    const sameTarget = target ? (!isNew && target.id === selectedId) : isNew;
    if (!dirty || !sameTarget) populateModal(target, false);
    else syncModalControls();
    modalWanted = true;
    syncModalVisibility();
    const focusTarget = isNew ? byId('mcp-server-name') : byId('mcp-modal-close');
    focusTarget?.focus({ preventScroll: true });
  }

  function closeModal() {
    modalWanted = false;
    syncModalVisibility();
    renderCards();
    const triggerCard = [...byId('mcp-cards').children].find(card => card.dataset.serverId === returnFocus?.dataset.serverId);
    const focusTarget = returnFocus?.isConnected ? returnFocus : triggerCard?.querySelector('.mcp-card-edit');
    focusTarget?.focus({ preventScroll: true });
  }

  function collectModalDraft() {
    const current = isNew ? null : servers.find(item => item.id === selectedId);
    const builtin = Boolean(current?.builtin);
    const customConfigText = byId('mcp-server-custom-config').value;
    const preserveCustomConfig = builtin || (current && customConfigText === formatKeyValue(current.custom_config, '='));
    const disabledTools = builtin
      ? [...byId('mcp-modal-tool-list').querySelectorAll('input')].filter(input => !input.checked).map(input => input.dataset.toolName)
      : parseLines(byId('mcp-server-disabled-tools').value);
    return {
      id: isNew ? newServerId : selectedId,
      name: builtin ? current.name : byId('mcp-server-name').value.trim(),
      category: current?.category || (builtin ? 'Antigravity 內建 MCP' : '自訂擴充 MCP'),
      description: builtin ? current.description || '' : byId('mcp-server-description').value.trim(),
      transport: builtin ? 'stdio' : byId('mcp-server-transport').value,
      command: byId('mcp-server-command').value.trim(),
      args: parseLines(byId('mcp-server-args').value),
      cwd: byId('mcp-server-cwd').value.trim(),
      env: parseKeyValue(byId('mcp-server-env').value, '='),
      url: byId('mcp-server-url').value.trim(),
      headers: parseKeyValue(byId('mcp-server-headers').value, ':'),
      enabled: byId('mcp-enabled').checked,
      read_only: byId('mcp-readonly').checked,
      disabled_tools: disabledTools,
      custom_config: preserveCustomConfig ? { ...(current.custom_config || {}) } : parseKeyValue(customConfigText, '='),
    };
  }

  function renderCards() {
    const container = byId('mcp-cards');
    if (!container) return;
    container.replaceChildren();
    if (!configuration && !servers.length) {
      const empty = document.createElement('div');
      empty.className = 'mcp-empty';
      empty.textContent = '正在讀取 MCP 服務卡片…';
      container.append(empty);
      return;
    }
    const builtinCount = servers.filter(item => item.builtin).length;
    const customCount = servers.length - builtinCount;
    const enabledCount = servers.filter(item => item.enabled).length;
    const summaryNode = byId('mcp-active-summary');
    if (summaryNode) {
      summaryNode.textContent = `${servers.length} 個服務 · ${enabledCount} 個已啟用 · 內建 ${builtinCount} / 自訂 ${customCount}`;
    }

    for (const server of servers) {
      const isSelectedDirty = dirty && !isNew && server.id === selectedId;
      const card = document.createElement('article');
      card.className = 'mcp-list-card';
      card.setAttribute('role', 'listitem');
      card.dataset.serverId = server.id;
      card.dataset.enabled = String(Boolean(server.enabled));

      const top = document.createElement('div');
      top.className = 'mcp-list-card-top';
      const title = document.createElement('h4');
      title.className = 'mcp-list-card-title';
      title.textContent = server.name;

      const badges = document.createElement('div');
      badges.className = 'mcp-list-card-badges';

      const statusBadge = document.createElement('span');
      statusBadge.className = 'mcp-badge';
      statusBadge.dataset.state = !server.enabled ? 'disabled' : server.read_only ? 'readonly' : 'enabled';
      statusBadge.textContent = !server.enabled ? '已停用' : server.read_only ? '已啟用 · 唯讀' : '已啟用';
      badges.append(statusBadge);

      if (isSelectedDirty) {
        const dirtyBadge = document.createElement('span');
        dirtyBadge.className = 'mcp-badge';
        dirtyBadge.dataset.state = 'dirty';
        dirtyBadge.textContent = '尚未儲存';
        badges.append(dirtyBadge);
      }
      top.append(title, badges);

      const desc = document.createElement('p');
      desc.className = 'mcp-list-card-desc';
      desc.textContent = server.description || '未填寫用途說明';
      desc.title = desc.textContent;

      const meta = document.createElement('div');
      meta.className = 'mcp-list-card-meta';
      const tools = server.tools || server.discovered_tools || (server.builtin ? configuration?.tools || [] : []);
      const totalTools = server.tool_count ?? tools.length;
      const disabledSet = new Set(server.disabled_tools || []);
      const enabledToolCount = !server.enabled ? 0 : tools.length
        ? tools.filter(tool => !disabledSet.has(tool.name) && !(server.read_only && tool.writes)).length
        : server.enabled_tool_count ?? 0;
      const toolSpan = document.createElement('span');
      toolSpan.textContent = totalTools || server.builtin || server.last_test?.ok
        ? `${enabledToolCount} / ${totalTools} 個工具可用`
        : '工具數量待確認';
      meta.append(toolSpan);

      const actions = document.createElement('div');
      actions.className = 'mcp-list-card-actions';

      const switchLabel = document.createElement('label');
      switchLabel.className = 'mcp-card-switch';
      const switchInput = document.createElement('input');
      switchInput.type = 'checkbox';
      switchInput.className = 'mcp-card-toggle';
      switchInput.dataset.serverId = server.id;
      switchInput.setAttribute('role', 'switch');
      switchInput.setAttribute('aria-label', `啟用 ${server.name}`);
      switchInput.checked = Boolean(server.enabled);
      switchInput.disabled = busy;
      const switchText = document.createElement('span');
      switchText.textContent = '啟用服務';
      switchLabel.append(switchInput, switchText);
      switchInput.addEventListener('change', event => {
        event.stopPropagation();
        void quickToggleServer(server, switchInput.checked);
      });
      switchLabel.addEventListener('click', event => event.stopPropagation());

      const btnGroup = document.createElement('div');
      btnGroup.className = 'settings-actions mcp-card-buttons';

      const testBtn = document.createElement('button');
      testBtn.type = 'button';
      testBtn.className = 'btn mcp-card-test';
      testBtn.dataset.serverId = server.id;
      testBtn.textContent = '測試連線';
      testBtn.setAttribute('aria-label', `測試連線：${server.name}`);
      testBtn.disabled = busy;
      testBtn.addEventListener('click', event => {
        event.stopPropagation();
        void testServer(server.id, testBtn);
      });

      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn btn-primary mcp-card-edit';
      editBtn.dataset.serverId = server.id;
      editBtn.textContent = '詳細設定';
      editBtn.setAttribute('aria-label', `詳細設定：${server.name}`);
      editBtn.addEventListener('click', event => {
        event.stopPropagation();
        mcpFeedback('');
        openModal(server, editBtn);
      });

      btnGroup.append(testBtn, editBtn);
      actions.append(switchLabel, btnGroup);
      card.append(top, desc, meta, actions);
      card.addEventListener('click', event => {
        if (event.target.closest('button, input, label, code')) return;
        editBtn.click();
      });
      container.append(card);
    }
  }

  function applyConfigUpdate(result) {
    if (!result) return;
    const nextConfig = result.config || result;
    configuration = {
      ...configuration,
      ...nextConfig,
      tools: nextConfig.tools || configuration?.tools || [],
    };
    if (Array.isArray(nextConfig.servers) && nextConfig.servers.length) {
      servers = normalizeServers(nextConfig);
    } else if (result.server) {
      const idx = servers.findIndex(item => item.id === result.server.id);
      if (idx >= 0) servers[idx] = { ...servers[idx], ...result.server };
      else servers.push(result.server);
    } else {
      servers = servers.map(item => item.id === 'gallery'
        ? {
            ...item,
            enabled: Boolean(configuration.mcp_enabled),
            read_only: Boolean(configuration.mcp_read_only),
            disabled_tools: [...(configuration.disabled_tools || [])],
          }
        : item);
    }
    updatePermissions();
    renderCards();
  }

  async function quickToggleServer(server, enabled) {
    if (busy) return;
    busy = true;
    renderCards();
    mcpFeedback('處理中…');
    try {
      let result;
      if (server.id === 'gallery') {
        result = await app.api('/api/settings/integrations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mcp_enabled: enabled, mcp_read_only: Boolean(server.read_only) }),
        });
      } else {
        result = await app.api(`/api/settings/mcp/servers/${encodeURIComponent(server.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...server, enabled }),
        });
      }
      applyConfigUpdate(result);
      if (!dirty && selectedId === server.id) {
        populateModal(servers.find(item => item.id === server.id), false);
      }
      mcpFeedback(`「${server.name}」已${enabled ? '啟用' : '停用'}並立即套用。`);
      app.emit('tools:permissions');
    } catch (error) {
      mcpFeedback(error.message, true);
      renderCards();
    } finally {
      busy = false;
      syncModalControls();
      renderCards();
    }
  }

  async function testServer(serverId, button) {
    const endpoint = (!serverId || serverId === 'gallery')
      ? '/api/settings/mcp/test'
      : `/api/settings/mcp/servers/${encodeURIComponent(serverId)}/test`;
    return action(button, 'mcp-feedback', async () => {
      const res = await app.api(endpoint, { method: 'POST' });
      feedback('mcp-page-feedback', res?.message || '測試完成。', res?.ok === false);
      return res;
    });
  }

  async function loadArtistSettings() {
    try {
      const data = await app.api('/api/settings/artist-identity');
      byId('artist-model-status').textContent = `已記住 ${data.identities} 位畫師 · ${data.pending_count || 0} 筆待補查 · 模型由 Antigravity 等 MCP 客戶端執行`;
      const list = byId('artist-model-requests'); list.replaceChildren();
      const states = { pending: '查證中', pending_model: '等待 MCP 模型補查', unresolved: '未找到足夠依據', retry: '待重試', conflict: '身分衝突' };
      for (const request of data.requests || []) {
        const row = document.createElement('div'); row.className = 'settings-section';
        const text = document.createElement('p'); text.textContent = `${request.artist_name || '平台帳號'} · ${states[request.status] || request.status}${request.last_error ? `：${request.last_error}` : ''}`;
        const button = document.createElement('button'); button.className = 'btn'; button.textContent = '重查 Danbooru';
        button.addEventListener('click', () => action(button, 'artist-model-feedback', async () => {
          await app.api('/api/settings/mcp/tools/gallery_resolve_artist_identity/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ arguments: {
            artist_name: request.artist_name, profile_urls: JSON.parse(request.profile_urls_json), force_refresh: true
          } }) }); return { message: '已重新查詢 Danbooru；查不到則保留於 MCP 待補查佇列，圖庫不啟動模型。' };
        })); row.append(text, button); list.append(row);
      }
    } catch (error) { feedback('artist-model-feedback', `畫師身分設定讀取失敗：${error.message}`, true); }
  }

  async function loadDedupSettings() {
    const statusNode = byId('dedup-model-status');
    if (!statusNode) return;
    try {
      const latest = await app.api('/api/dedup/scans/latest');
      const data = latest.scan?.result;
      if (latest.scan?.status === 'running' || latest.scan?.status === 'queued') {
        const p = latest.scan.progress || {};
        statusNode.textContent = `掃描進行中：${p.stage || '準備中'} · ${p.processed_count || 0} / ${p.total_target || 0}，開啟清理視窗可接續查看。`;
        return;
      }
      if (typeof data?.total_groups === 'number') {
        statusNode.textContent = `最近掃描結果：共 ${data.total_groups} 組完全重複圖片（完全重複 ${data.exact_groups_count || 0} 組 · 像素完全相同 ${data.visual_groups_count || 0} 組 · 可清理 ${data.total_duplicate_images || 0} 張 · 預計釋放 ${data.total_reclaim_mb || 0} MB） · MCP 工具：gallery_clean_duplicates`;
      } else {
        statusNode.textContent = 'MCP 工具：gallery_clean_duplicates · 可直接開啟預覽與清理視窗';
      }
    } catch (error) {
      statusNode.textContent = 'MCP 工具：gallery_clean_duplicates · 可直接開啟預覽與清理視窗';
    }
  }

  async function loadSettings(force = false) {
    if (loading) return loading;
    if (configuration && !force) return configuration;
    for (const control of byId('settings-mcp').querySelectorAll('input, button, select')) control.disabled = true;
    if (byId('mcp-modal')) {
      for (const control of byId('mcp-modal').querySelectorAll('input, button, select, textarea')) control.disabled = true;
    }
    loading = app.api('/api/settings/integrations').then(data => {
      configuration = {
        ...data,
        disabled_tools: [...(data.disabled_tools || [])],
        tools: [...(data.tools || [])],
      };
      servers = normalizeServers(configuration);

      const modalList = byId('mcp-modal-tool-list');
      modalList.replaceChildren();

      for (const tool of configuration.tools) {
        const mLabel = document.createElement('label'); mLabel.className = 'settings-check';
        const mInput = document.createElement('input'); mInput.type = 'checkbox'; mInput.dataset.toolName = tool.name; mInput.dataset.writes = String(tool.writes);
        mInput.checked = !configuration.disabled_tools.includes(tool.name);
        const mText = document.createElement('span'); mText.textContent = `${tool.label} · ${tool.writes ? '可修改圖庫' : '唯讀'}`;
        mLabel.append(mInput, mText); modalList.append(mLabel);
      }

      if (!dirty) {
        const target = servers.find(item => item.id === selectedId) || servers[0];
        populateModal(target, false);
      } else {
        syncModalControls();
        renderCards();
      }
      byId('mcp-session-status').textContent = data.sessions?.length
        ? `最近工具存取：${new Date(data.sessions[0].last_seen * 1000).toLocaleString('zh-TW')}（PID ${data.sessions[0].pid}）`
        : '尚未收到 MCP 客戶端的工具存取。';
      if (data.config_error) mcpFeedback(data.config_error, true);
    }).catch(error => {
      mcpFeedback(`設定讀取失敗：${error.message}，請重新進入此頁重試。`, true);
    }).finally(() => {
      loading = null;
      for (const control of byId('settings-mcp').querySelectorAll('input, button, select')) control.disabled = !configuration;
      if (byId('mcp-modal')) {
        for (const control of byId('mcp-modal').querySelectorAll('input, button, select, textarea')) control.disabled = !configuration;
      }
      syncModalControls();
      renderCards();
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
    const draft = collectModalDraft();
    const current = isNew ? null : servers.find(item => item.id === selectedId);
    if (!draft.name) {
      mcpFeedback('請填寫 MCP 顯示名稱。', true);
      byId('mcp-server-name').focus();
      return;
    }
    if (!current?.builtin && draft.transport === 'stdio' && !draft.command) {
      mcpFeedback('請填寫啟動指令。', true);
      byId('mcp-server-command').focus();
      return;
    }
    if (!current?.builtin && draft.transport !== 'stdio' && !/^https?:\/\//i.test(draft.url)) {
      mcpFeedback('請填寫 http:// 或 https:// 開頭的連線網址。', true);
      byId('mcp-server-url').focus();
      return;
    }

    const result = await action(button, 'mcp-feedback', async () => {
      busy = true;
      syncModalControls();
      try {
        if (!isNew && selectedId === 'gallery') {
          const metaChanged = current && (
            draft.name !== current.name ||
            draft.category !== (current.category || 'Antigravity 內建 MCP') ||
            draft.description !== current.description ||
            draft.command !== (current.command || 'python') ||
            JSON.stringify(draft.args) !== JSON.stringify(current.args || ['mcp_server.py']) ||
            draft.cwd !== (current.cwd || '') ||
            JSON.stringify(draft.env) !== JSON.stringify(current.env || {}) ||
            JSON.stringify(draft.custom_config) !== JSON.stringify(current.custom_config || {})
          );
          const toolsChanged = JSON.stringify([...draft.disabled_tools].sort()) !== JSON.stringify([...(current?.disabled_tools || configuration?.disabled_tools || [])].sort());
          if (!metaChanged) {
            const payload = { mcp_enabled: draft.enabled, mcp_read_only: draft.read_only };
            if (toolsChanged) payload.disabled_tools = draft.disabled_tools;
            return await app.api('/api/settings/integrations', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
          }
        }
        const url = isNew ? '/api/settings/mcp/servers' : `/api/settings/mcp/servers/${encodeURIComponent(selectedId)}`;
        const method = isNew ? 'POST' : 'PUT';
        return await app.api(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draft),
        });
      } finally {
        busy = false;
      }
    });
    if (result) {
      const wasNew = isNew;
      if (wasNew) {
        const created = result.server || { ...draft, server_name: draft.id, builtin: false };
        if (!Array.isArray(result.config?.servers)) {
          servers = servers.filter(item => item.id !== created.id).concat(created);
        }
      } else if (!Array.isArray(result.servers) && !Array.isArray(result.config?.servers) && selectedId !== 'gallery') {
        servers = servers.map(item => item.id === selectedId ? { ...item, ...(result.server || draft) } : item);
      }
      applyConfigUpdate(result);
      selectedId = draft.id;
      isNew = false;
      dirty = false;
      populateModal(servers.find(item => item.id === selectedId) || servers[0], false);
      mcpFeedback(result.message || '權限已儲存並立即套用。客戶端請重新整理 MCP 工具清單。');
      app.emit('tools:permissions');
    } else {
      syncModalControls();
    }
  }

  async function deleteCurrentServer(button) {
    if (isNew || !selectedId) return;
    const current = servers.find(item => item.id === selectedId);
    if (!current || current.builtin) return;
    const result = await action(button, 'mcp-feedback', async () => {
      busy = true;
      syncModalControls();
      try {
        return await app.api(`/api/settings/mcp/servers/${encodeURIComponent(selectedId)}`, { method: 'DELETE' });
      } finally {
        busy = false;
      }
    });
    if (result) {
      if (Array.isArray(result.config?.servers)) {
        applyConfigUpdate(result);
      } else {
        servers = servers.filter(item => item.id !== selectedId);
      }
      selectedId = 'gallery';
      dirty = false;
      populateModal(servers[0] || null, false);
      closeModal();
      mcpFeedback(result.message || `已刪除 MCP 卡片「${current.name}」。`);
    } else {
      syncModalControls();
    }
  }

  function renderJobs(runtime) {
    const jobs = runtime.jobs || [], active = runtime.active_job;
    const health = runtime.library_health;
    if (health) byId('tasks-library-health').textContent = `保留待確認遺失 ${health.missing} 張 · 檔案指紋 ${health.hashed} / ${health.total} · 待重試處理 ${health.failed_stages} 項`;
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
    addPanel('mcp', `
      <div class="mcp-header">
        <div class="mcp-heading">
          <h3>服務連線</h3>
          <p class="settings-note">管理 Antigravity 與自訂 MCP 的連線、存取模式及工具權限。</p>
        </div>
        <div class="mcp-toolbar">
          <button id="mcp-page-export" type="button" class="btn">匯出全部連線設定</button>
          <button id="mcp-page-sync" type="button" class="btn">同步寫入 Antigravity</button>
          <button id="mcp-card-new" type="button" class="btn btn-primary"><span aria-hidden="true">＋</span> 新增 MCP</button>
        </div>
      </div>
      <div class="mcp-overview">
        <p id="mcp-active-summary" class="mcp-summary-line">讀取中…</p>
        <p id="mcp-session-status" class="settings-note"></p>
      </div>
      <p id="mcp-page-feedback" class="settings-feedback" role="status" aria-live="polite"></p>
      <div id="mcp-cards" class="mcp-cards" role="list" aria-label="MCP 服務卡片列表"></div>`);

    const modal = document.createElement('div');
    modal.id = 'mcp-modal';
    modal.className = 'modal-backdrop';
    modal.hidden = true;
    modal.innerHTML = `
      <div class="dialog-box mcp-dialog" role="dialog" aria-modal="true" aria-labelledby="mcp-modal-title" tabindex="-1">
        <div class="mcp-modal-header">
          <div class="mcp-modal-heading">
            <p class="mcp-modal-eyebrow" id="mcp-modal-eyebrow">MCP 管理 · 服務連線與權限設定</p>
            <h3 id="mcp-modal-title">調整 MCP 設定</h3>
          </div>
          <div class="mcp-modal-header-actions">
            <button id="mcp-modal-close" type="button" class="btn" aria-label="關閉 MCP 設定視窗">✕</button>
          </div>
        </div>
        <div id="mcp-modal-body" class="mcp-modal-body">
          <form id="mcp-form" class="mcp-editor" novalidate>
            <p id="mcp-modal-description" class="settings-note"></p>
            <label id="mcp-name-field">服務名稱<input id="mcp-server-name" class="search-input" required maxlength="100" placeholder="例如：ComfyUI 工作流"></label>
            <section class="mcp-access" aria-label="存取權限">
              <label class="mcp-toggle-row">
                <span><strong>啟用服務</strong><small>允許客戶端呼叫此 MCP。</small></span>
                <input id="mcp-enabled" type="checkbox" role="switch" aria-label="啟用服務">
              </label>
              <label class="mcp-toggle-row">
                <span><strong>唯讀模式</strong><small id="mcp-readonly-note">只允許查詢，不允許修改圖庫。</small></span>
                <input id="mcp-readonly" type="checkbox" role="switch" aria-label="唯讀模式">
              </label>
            </section>
            <div id="mcp-custom-connection" class="mcp-section-card" hidden></div>
            <details class="mcp-details" id="mcp-modal-tools-details">
              <summary><span>工具權限</span><span class="mcp-detail-summary" id="mcp-modal-tools-summary"></span></summary>
              <div class="mcp-details-body">
                <div id="mcp-modal-tools-builtin" class="mcp-field-stack">
                  <p class="settings-note">取消勾選即可停用工具。唯讀模式會自動停用修改圖庫的工具。</p>
                  <div id="mcp-modal-tool-list" class="mcp-modal-tool-list"></div>
                </div>
                <div id="mcp-modal-tools-custom" hidden>
                  <label>停用工具<textarea id="mcp-server-disabled-tools" class="search-input" rows="3" spellcheck="false" placeholder="每行一個工具名稱"></textarea></label>
                </div>
              </div>
            </details>
            <details class="mcp-details" id="mcp-modal-advanced-details">
              <summary><span>進階設定</span><span class="mcp-detail-summary" id="mcp-advanced-summary">啟動路徑、環境變數</span></summary>
              <div class="mcp-details-body">
                <div id="mcp-advanced-connection">
                  <section id="mcp-connection-settings" class="mcp-field-stack" aria-label="連線設定">
                    <p id="mcp-connection-note" class="settings-note">本機連線已自動配置；路徑變更時才需調整。</p>
                    <label id="mcp-transport-field">連線方式
                      <select id="mcp-server-transport" class="select-box">
                        <option value="stdio">本機程式（stdio）</option>
                        <option value="http">HTTP</option>
                        <option value="sse">SSE</option>
                      </select>
                    </label>
                    <div id="mcp-stdio-fields" class="mcp-field-stack">
                      <label>啟動指令<input id="mcp-server-command" class="search-input" spellcheck="false" placeholder="例如：python、node、npx"></label>
                      <label>啟動參數<textarea id="mcp-server-args" class="search-input" rows="2" spellcheck="false" placeholder="每行一個參數，例如 mcp_server.py"></textarea></label>
                    </div>
                    <div id="mcp-http-fields" hidden>
                      <label>連線網址<input id="mcp-server-url" class="search-input" type="url" spellcheck="false" placeholder="http://127.0.0.1:8188/mcp"></label>
                    </div>
                  </section>
                </div>
                <div id="mcp-stdio-options" class="mcp-field-stack">
                  <label>工作目錄（選填）<input id="mcp-server-cwd" class="search-input" spellcheck="false" placeholder="使用預設工作目錄"></label>
                  <label>環境變數（選填）<textarea id="mcp-server-env" class="search-input" rows="2" spellcheck="false" placeholder="每行 KEY=VALUE"></textarea></label>
                </div>
                <div id="mcp-http-options" hidden>
                  <label>請求標頭（選填）<textarea id="mcp-server-headers" class="search-input" rows="2" spellcheck="false" placeholder="每行 Header-Name: Value"></textarea></label>
                </div>
                <div id="mcp-custom-fields" class="mcp-field-stack" hidden>
                  <label>用途說明（選填）<textarea id="mcp-server-description" class="search-input" rows="2" maxlength="1000" placeholder="簡短說明服務用途"></textarea></label>
                  <label>擴充參數（選填）<textarea id="mcp-server-custom-config" class="search-input" rows="3" spellcheck="false" placeholder="每行 key=value，例如 timeout_ms=15000"></textarea></label>
                </div>
                <div id="mcp-sidecar-fields" class="mcp-field-stack" hidden>
                  <p class="settings-note">背景查證使用 Gemini 3.8 Flash。需要重新配置時，可更新 Sidecar 設定。</p>
                  <div class="settings-actions"><button id="mcp-sidecar-install" type="button" class="btn">更新 Sidecar 設定</button></div>
                </div>
                <button id="mcp-delete" type="button" class="btn btn-danger" hidden>刪除此服務</button>
              </div>
            </details>
            <details class="mcp-details" id="mcp-modal-client-details">
              <summary><span>連接 Antigravity</span><span class="mcp-detail-summary">同步與匯出</span></summary>
              <div class="mcp-details-body">
                <p class="settings-note">將此服務加入本機 Antigravity，或複製設定手動匯入。同步時會自動備份設定檔。</p>
                <div class="settings-actions">
                  <button id="mcp-sync-client" type="button" class="btn">同步至 Antigravity</button>
                  <button id="mcp-export" type="button" class="btn">匯出連線設定</button>
                  <button id="mcp-copy" type="button" class="btn" hidden>複製設定</button>
                </div>
                <textarea id="mcp-client-config" class="search-input" rows="6" readonly aria-label="MCP 客戶端連線設定" hidden></textarea>
                <p id="mcp-export-note" class="settings-note" hidden></p>
              </div>
            </details>
          </form>
        </div>
        <div id="mcp-modal-footer" class="mcp-modal-footer">
          <div class="mcp-modal-footer-info">
            <span id="mcp-draft-state" role="status" aria-live="polite" data-state="saved">已儲存</span>
            <p id="mcp-feedback" class="settings-feedback" role="status" aria-live="polite"></p>
          </div>
          <div class="settings-actions">
            <button id="mcp-test" type="button" class="btn">測試連線</button>
            <button id="mcp-save" type="button" class="btn btn-primary">儲存變更</button>
          </div>
        </div>
      </div>`;
    document.body.append(modal);

    addPanel('tools', '');
    addPanel('tasks', `<div><h3>背景任務與復原</h3><p id="tasks-current" role="status">讀取中…</p><p id="tasks-library-health" class="settings-note"></p><p class="settings-note">任務狀態跨 Web、Windows 排程與 MCP 共用。重試會依處理狀態選擇未完成項目。</p></div>
      <div class="settings-actions"><button id="tasks-recover" class="btn">檢查並復原中斷的檔案操作</button><button id="tasks-backup" class="btn">備份資料庫</button></div><p id="tasks-feedback" class="settings-feedback" role="status"></p><div id="tasks-history"></div>`);
    addPanel('artists', `<div><h3>畫師跨平台身分</h3><p class="settings-note">智慧標籤先查 Danbooru；查不到時列入 MCP 待補查佇列。由 Antigravity 搜尋作者頁面並回寫查證結果，確認後記住別名，使用同一個資料夾。未確認的圖片暫緩自動歸檔。</p></div>
      <p class="settings-note">模型登入與選擇在 Antigravity 管理。圖庫不啟動模型 CLI，也不需要 API 金鑰。請先在「MCP 管理」產生 Antigravity 連線設定，再將下方指令貼到已連線的 Antigravity 對話。</p>
      <label>Antigravity 任務請求<textarea id="artist-mcp-prompt" class="search-input" rows="5" readonly>請處理本圖庫的待補查畫師身分，完成後回報確認與略過數量。</textarea></label>
      <div class="settings-actions"><button id="artist-model-copy" class="btn btn-primary">複製補查指令</button><button id="artist-model-refresh" class="btn">重新整理查證狀態</button></div>
      <p id="artist-model-feedback" class="settings-feedback" role="status"></p><p id="artist-model-status" class="settings-note"></p><div id="artist-model-requests"></div>`);
    addPanel('dedup', `<div><h3>完全重複圖片清理</h3><p class="settings-note">支援「完全重複檔案（SHA-256 100% 相同＋位元組驗證）」與「靜態圖像素完全比對（OpenCV 原尺寸、所有色彩通道與透明度逐像素一致，差異像素須為 0，排除動圖）」。清理時依像素數、格式與檔案大小等固定排序保留版本、繼承全部核心與人工標籤，先備份資料庫再將重複圖片移至資源回收桶。</p></div>
      <p class="settings-note">可直接點擊下方按鈕開啟圖庫內建預覽與清理視窗，或將指令貼到已連線圖庫 MCP 的 Antigravity 對話執行。</p>
      <label>Antigravity 預覽請求<textarea id="dedup-mcp-prompt" class="search-input" rows="5" readonly>請預覽本圖庫同資料夾內檔案或原尺寸像素完全相同的圖片，回報候選數量與預計可釋放空間。</textarea></label>
      <div class="settings-actions"><button id="dedup-open-modal" type="button" class="btn btn-primary">🧹 開啟清除重複圖片視窗（預覽與執行）</button><button id="dedup-model-copy" type="button" class="btn">複製清理指令</button><button id="dedup-model-refresh" type="button" class="btn">重新整理重複統計</button></div>
      <p id="dedup-model-feedback" class="settings-feedback" role="status"></p><p id="dedup-model-status" class="settings-note">MCP 工具：gallery_clean_duplicates</p>`);

    window.openMcpModal = serverId => openModal(serverId === null ? null : (serverId || 'gallery'));
    window.closeMcpModal = closeModal;
    window.loadArtistSettings = loadArtistSettings;
    window.loadDedupSettings = loadDedupSettings;

    modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
    byId('mcp-modal-close').addEventListener('click', closeModal);

    byId('mcp-card-new').addEventListener('click', event => {
      mcpFeedback('');
      openModal(null, event.currentTarget);
    });

    byId('mcp-page-export').addEventListener('click', event => {
      const trigger = event.currentTarget;
      openModal(selectedId || 'gallery', trigger);
      byId('mcp-modal-client-details').open = true;
      void action(byId('mcp-export'), 'mcp-feedback', async () => {
        const value = await app.api('/api/settings/mcp/client_config?include_all=true');
        byId('mcp-client-config').value = JSON.stringify(value, null, 2);
        for (const id of ['mcp-client-config', 'mcp-copy', 'mcp-export-note']) byId(id).hidden = false;
        byId('mcp-export-note').textContent = '在 Antigravity 的 MCP 管理開啟設定，將內容合併至 mcp_config.json 的 mcpServers 欄位；保留既有伺服器。';
        feedback('mcp-page-feedback', '已產生全部 Antigravity MCP 連線設定。');
        return { message: '已產生 Antigravity 連線設定。' };
      });
    });

    byId('mcp-page-sync').addEventListener('click', event => action(event.currentTarget, 'mcp-page-feedback', async () => {
      const res = await app.api('/api/settings/mcp/sync_client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include_all: true }),
      });
      feedback('mcp-feedback', res?.message || '已同步寫入 Antigravity 設定檔。');
      return res;
    }));

    byId('mcp-form').addEventListener('input', () => {
      dirty = true;
      mcpFeedback('');
      syncModalControls();
      renderCards();
    });
    byId('mcp-form').addEventListener('change', () => {
      dirty = true;
      syncModalControls();
      renderCards();
    });
    byId('mcp-form').addEventListener('submit', event => {
      event.preventDefault();
      void save(byId('mcp-save'));
    });

    byId('artist-model-copy').addEventListener('click', event => action(event.currentTarget, 'artist-model-feedback', async () => {
      await navigator.clipboard.writeText(byId('artist-mcp-prompt').value); return { message: '已複製，請貼到已連線圖庫 MCP 的 Antigravity 對話。' };
    }));
    byId('artist-model-refresh').addEventListener('click', () => { void loadArtistSettings(); });
    byId('dedup-open-modal').addEventListener('click', () => {
      if (typeof window.openDedupModal === 'function') window.openDedupModal();
    });
    byId('dedup-model-copy').addEventListener('click', event => action(event.currentTarget, 'dedup-model-feedback', async () => {
      await navigator.clipboard.writeText(byId('dedup-mcp-prompt').value); return { message: '已複製清理指令，請貼到已連線圖庫 MCP 的 Antigravity 對話。' };
    }));
    byId('dedup-model-refresh').addEventListener('click', () => { void loadDedupSettings(); });
    byId('mcp-save').addEventListener('click', event => save(event.currentTarget));
    byId('mcp-delete').addEventListener('click', event => deleteCurrentServer(event.currentTarget));
    byId('mcp-test').addEventListener('click', event => testServer(selectedId, event.currentTarget));
    byId('mcp-export').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      const url = (!selectedId || selectedId === 'gallery')
        ? '/api/settings/mcp/client_config'
        : `/api/settings/mcp/client_config?server_id=${encodeURIComponent(selectedId)}`;
      const value = await app.api(url);
      byId('mcp-client-config').value = JSON.stringify(value, null, 2);
      for (const id of ['mcp-client-config', 'mcp-copy', 'mcp-export-note']) byId(id).hidden = false;
      byId('mcp-export-note').textContent = '在 Antigravity 的 MCP 管理開啟設定，將內容合併至 mcp_config.json 的 mcpServers 欄位；保留既有伺服器。';
      return { message: '已產生 Antigravity 連線設定。' };
    }));
    byId('mcp-sync-client').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      const payload = selectedId ? { server_id: selectedId } : { include_all: true };
      const res = await app.api('/api/settings/mcp/sync_client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      feedback('mcp-page-feedback', res?.message || '已同步寫入本機設定檔。');
      return res;
    }));
    byId('mcp-sidecar-install').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      const res = await app.api('/api/settings/mcp/sidecar/install', { method: 'POST' });
      return { message: `已安裝／更新 Sidecar 設定（${res.server_name || 'gallerypub'}）。` };
    }));
    byId('mcp-copy').addEventListener('click', event => action(event.currentTarget, 'mcp-feedback', async () => {
      await navigator.clipboard.writeText(byId('mcp-client-config').value); return { message: '已複製。' };
    }));
    byId('tasks-recover').addEventListener('click', event => action(event.currentTarget, 'tasks-feedback', async () => {
      const result = await app.api('/api/maintenance/recover', { method: 'POST' });
      if (result.errors?.length) throw new Error(result.errors.map(item => item.error).join('；'));
      return { message: `復原檢查完成，處理 ${result.recovered.length} 個中斷批次。` };
    }));
    byId('tasks-backup').addEventListener('click', event => action(event.currentTarget, 'tasks-feedback', () => app.api('/api/maintenance/backup', { method: 'POST' })));

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
        if (!byId('mcp-save').disabled) void save(byId('mcp-save'));
        return;
      }
      if (event.key === 'Tab') {
        const focusable = [...modal.querySelectorAll('button, input, select, textarea, summary, [tabindex="0"]')]
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

    app.use('switchSettingsTab', (next, section) => {
      const result = next(section);
      syncModalVisibility();
      if (section === 'mcp' || section === 'tools' || section === 'artists' || section === 'dedup') void loadSettings();
      if (section === 'tools' || section === 'artists') {
        if (byId('artist-workflow-details') && section === 'artists') byId('artist-workflow-details').open = true;
        if (byId('settings-artists')) byId('settings-artists').hidden = false;
        void loadArtistSettings();
      }
      if (section === 'tools' || section === 'dedup') {
        if (byId('dedup-workflow-details') && section === 'dedup') byId('dedup-workflow-details').open = true;
        if (byId('settings-dedup')) byId('settings-dedup').hidden = false;
        void loadDedupSettings();
      }
      if (section === 'tasks') { void app.refreshRuntime().catch(error => feedback('tasks-feedback', error.message, true)); byId('settings-footer-note').textContent = '檔案復原與備份會取得共用作業鎖。'; }
      return result;
    });
    app.use('closeScheduleModal', (next, ...args) => {
      closeModal();
      return next(...args);
    });
    app.on('workspace:change', () => {
      if (app.runtime.navigation && app.runtime.navigation.workspace !== 'ai') modalWanted = false;
      syncModalVisibility();
      if (app.runtime.navigation.isVisible('artists')) void loadArtistSettings();
      if (app.runtime.navigation.isVisible('dedup')) void loadDedupSettings();
      if (app.runtime.navigation.isVisible('mcp') || app.runtime.navigation.isVisible('tools')) void loadSettings();
    });
    app.on('runtime', renderJobs);
    app.on('tools:permissions', config => {
      if (!config) return;
      const previousDisabled = configuration?.disabled_tools || [];
      const unchangedInputs = [...byId('mcp-modal-tool-list').querySelectorAll('input')]
        .filter(input => input.checked === !previousDisabled.includes(input.dataset.toolName));
      applyConfigUpdate(config);
      if (selectedId === 'gallery') {
        for (const input of unchangedInputs) {
          input.checked = !configuration.disabled_tools.includes(input.dataset.toolName);
        }
        updatePermissions();
      }
    });
    app.on('runtime:error', message => { byId('tasks-current').textContent = `任務狀態暫時無法讀取：${message}`; });
    renderCards();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
