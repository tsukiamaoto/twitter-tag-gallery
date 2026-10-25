/* Scheduled import owns its draft; live status refreshes never reload the form. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id);

  function initialize() {
    const page = byId('schedule-workspace');
    if (!page) return;
    let loaded = false, loading = false, requested = false, savedPayload = '', dirty = false;
    let status = {}, refreshPending = false, lastSources = '', secondSlotOpen = false;
    const options = [
      ['sch-run-pixiv', 'Pixiv 查詢'], ['sch-run-ai', 'AI 打標'],
      ['sch-run-rev', '以圖搜圖'], ['sch-auto-folder', '畫師歸檔'],
      ['sch-auto-rename', '自動改名'], ['sch-rm-empty', '清理空資料夾'],
      ['sch-also-root', '整理根目錄'],
    ];
    const sources = () => byId('sch-source-dirs').value.split('\n').map(value => value.trim()).filter(Boolean);
    const visible = () => app.runtime.navigation.workspace === 'schedule';
    const signature = payload => JSON.stringify({
      ...(payload || window.collectSchedulePayload()),
      schedule_times: payload ? [payload.schedule_times[0] || '', payload.schedule_times[1] || ''] : [byId('sch-time-1').value, byId('sch-time-2').value],
      secondSlotOpen: payload ? payload.schedule_times.length > 1 : secondSlotOpen,
    });

    function render() {
      const payload = window.collectSchedulePayload(), dirs = sources();
      dirty = loaded && signature() !== savedPayload;
      byId('schedule-second-slot').hidden = !secondSlotOpen;
      byId('schedule-add-time').hidden = secondSlotOpen;
      byId('sch-time-2').required = secondSlotOpen;
      byId('schedule-source-count').textContent = String(dirs.length);
      byId('schedule-source-summary').textContent = dirs.length ? `${dirs.length} 個資料夾` : '尚未設定';
      byId('schedule-frequency').textContent = `每日 ${payload.schedule_times.length} 次`;
      byId('schedule-executor').textContent = payload.sync_windows_task ? 'Windows 工作排程器' : '圖庫服務';
      byId('schedule-preview-label').textContent = dirty ? '儲存後生效' : '目前設定';
      const enabledOptions = options.filter(([id]) => byId(id).checked);
      byId('schedule-options-summary').textContent = enabledOptions.length ? `已啟用 ${enabledOptions.length} 項自動處理` : '僅搬入檔案';
      byId('schedule-flow-tag').hidden = !payload.run_pixiv_lookup && !payload.run_ai_tagger && !payload.run_reverse_search;
      byId('schedule-flow-organize').hidden = !payload.auto_create_artist_folders;
      byId('schedule-draft-status').textContent = loading ? '正在讀取設定…' : !loaded ? '設定尚未載入' : dirty ? '有尚未儲存的變更' : '所有變更已儲存';
      byId('schedule-draft-status').dataset.dirty = String(dirty);

      const state = byId('schedule-state');
      const savedEnabled = !!status.config?.enabled;
      state.dataset.state = loading ? 'loading' : !loaded ? 'error' : status.running_job ? 'running' : savedEnabled ? 'enabled' : 'paused';
      state.lastElementChild.textContent = loading ? '讀取中' : !loaded ? '載入失敗' : status.running_job ? '執行中' : savedEnabled ? '排程已啟用' : '排程已暫停';
      page.classList.toggle('schedule-is-running', !!status.running_job);

      let nextTime = '—', note = '讀取排程設定中…';
      if (loaded && !payload.enabled) { nextTime = '已暫停'; note = '開啟每日排程並儲存，即可自動匯入。'; }
      else if (loaded && !byId('sch-time-1').value) { nextTime = '尚未設定'; note = '請設定第一個執行時段。'; }
      else if (loaded && !dirs.length) { nextTime = '尚未就緒'; note = '加入至少一個來源資料夾，即可完成設定。'; }
      else if (loaded) {
        const now = new Date();
        const candidates = payload.schedule_times.filter(time => /^\d{2}:\d{2}$/.test(time)).map(time => {
          const [hours, minutes] = time.split(':').map(Number), next = new Date(now);
          next.setHours(hours, minutes, 0, 0);
          if (next <= now) next.setDate(next.getDate() + 1);
          return next;
        }).sort((a, b) => a - b);
        if (candidates.length) {
          const next = candidates[0];
          nextTime = next.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', hour12: false });
          note = `${next.getDate() === now.getDate() ? '今天' : '明天'} · ${dirty ? '依目前草稿預覽，儲存後生效' : '依本機時間執行'}`;
        }
      }
      byId('schedule-next-time').textContent = nextTime;
      byId('schedule-next-note').textContent = note;
    }

    page.addEventListener('input', event => {
      if (event.target.id === 'sch-source-dirs') {
        byId('sch-source-preview').style.display = 'none';
        byId('sch-folder-picker-status').textContent = '';
      }
      render();
    });
    page.addEventListener('change', render);
    byId('schedule-add-time').addEventListener('click', () => byId('sch-time-2').focus());
    byId('picker-clear-2').addEventListener('click', () => byId('schedule-add-time').focus());
    byId('schedule-retry').addEventListener('click', () => window.loadScheduleConfig());
    byId('schedule-history-more').addEventListener('click', () => {
      const button = byId('schedule-history-more'), box = byId('sch-history-box');
      const expanded = button.getAttribute('aria-expanded') !== 'true';
      box.dataset.expanded = String(expanded);
      box.querySelectorAll('.schedule-history-entry').forEach((entry, index) => { entry.hidden = !expanded && index >= 3; });
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收合較早紀錄' : `查看全部 ${box.children.length} 次紀錄`;
      byId('schedule-history-count').textContent = expanded ? `共 ${box.children.length} 次紀錄` : `最近 3 次 · 共 ${box.children.length} 次`;
      if (!expanded) byId('schedule-history-title').scrollIntoView({ block: 'nearest' });
    });
    byId('schedule-refresh-status').addEventListener('click', async () => {
      if (refreshPending) return;
      refreshPending = true;
      const button = byId('schedule-refresh-status'); button.disabled = true;
      try {
        window.renderScheduleHistory(await app.api('/api/schedule/status'));
      } catch (error) {
        byId('sch-live-status').textContent = `更新失敗：${error.message}`;
      } finally { refreshPending = false; button.disabled = false; }
    });
    app.on('schedule:form', change => {
      if (change?.slot === 2) secondSlotOpen = !!change.value;
      const nextSources = sources().join('\n');
      if (lastSources !== nextSources) byId('sch-source-preview').style.display = 'none';
      lastSources = nextSources;
      render();
    });
    app.on('schedule:loading', () => {
      loading = true; byId('schedule-form').disabled = true; byId('schedule-retry').hidden = true; render();
    });
    app.on('schedule:loaded', config => {
      loaded = true; loading = false; status.config = config;
      savedPayload = signature();
      byId('schedule-form').disabled = false; render();
    });
    app.on('schedule:load-error', () => {
      loading = false; byId('schedule-retry').hidden = false; render();
    });
    app.on('schedule:saved', ({ config, payload }) => {
      savedPayload = signature(payload); status.config = config; render();
    });
    app.on('schedule:status', value => { status = value; render(); });
    app.on('workspace:change', () => {
      if (!visible()) return;
      if (!requested) { requested = true; void window.loadScheduleConfig(); }
      else render();
    });
    byId('schedule-timezone').textContent = `· ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
    // Keep the next-run preview current without network requests or draft changes.
    setInterval(() => { if (!document.hidden && visible() && loaded) render(); }, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
