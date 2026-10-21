/* Progressive UI shell. Existing controls keep their original handlers. */
(() => {
  'use strict';
  if (GalleryApp.runtime.shellInstalled) return;
  GalleryApp.runtime.shellInstalled = true;
  const paths = {
    gallery: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="m3 16 5-5 4 4 3-3 6 6"/><circle cx="15.5" cy="8" r="1.5"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    compact: '<path d="M3 3h4v4H3zM10 3h4v4h-4zM17 3h4v4h-4zM3 10h4v4H3zM10 10h4v4h-4zM17 10h4v4h-4zM3 17h4v4H3zM10 17h4v4h-4zM17 17h4v4h-4z"/>',
    inbox: '<path d="M4 4h16l2 12v4H2v-4L4 4Z"/><path d="M2 15h6l2 3h4l2-3h6"/>',
    folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/><path d="m20 2 .5 1.5L22 4l-1.5.5L20 6l-.5-1.5L18 4l1.5-.5L20 2Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="currentColor" stroke="none"/><circle cx="15" cy="17" r="3" fill="currentColor" stroke="none"/>',
    tools: '<rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V4h8v3M3 12h18M10 12v3h4v-3"/>',
    chevron: '<path d="m7 10 5 5 5-5"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    reset: '<path d="M3 10a9 9 0 1 1 1.5 7M3 4v6h6"/>',
    filter: '<path d="M4 5h16M7 12h10M10 19h4"/>',
    star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>',
  };
  const icon = name => `<svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.gallery}</svg>`;
  const byId = id => document.getElementById(id);
  const mobile = window.matchMedia('(max-width: 800px)');
  let drawerTrigger = null;
  let drawerOpen = false;
  let loadingSequence = 0;
  let smartTaskBusy = false;
  let smartStartPending = false;
  let smartStopPending = false;
  let smartTaskCompleted = false;
  let modelActionPending = false;
  let latestModelStatus = null;
  let smartReturnFocus = null;
  let returnToSmartTag = false;
  let folderPreferences = { pinned: [], recent: [], sort: 'name' };
  let foldersReady = false;
  let folderLoadError = '';
  let folderAllExpanded = false;
  let folderVisibleLimit = 100;
  let lastFolderQuery = '';
  let folderFocusRequest = null;
  const folderCollator = new Intl.Collator('zh-Hant', { numeric: true, sensitivity: 'base' });

  function activateWithKeyboard(element) {
    element.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        element.click();
      }
    });
  }

  function labelButton(button, symbol, text, className = '') {
    if (!button) return;
    button.classList.add(...className.split(' ').filter(Boolean));
    button.innerHTML = `${icon(symbol)}<span class="shell-action-label">${text}</span>`;
    button.setAttribute('aria-label', text);
    button.title = text;
  }

  function setDensity(value, persist = true) {
    const density = value === 'compact' ? 'compact' : 'comfortable';
    document.body.dataset.galleryDensity = density;
    document.querySelectorAll('[data-density]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.density === density));
    });
    if (persist) {
      try { localStorage.setItem('gallery.shell.density', density); } catch { /* Browsing works without storage. */ }
    }
  }

  function setDrawer(open) {
    const sidebar = document.querySelector('.shell-sidebar');
    const isOpen = mobile.matches && open;
    drawerOpen = isOpen;
    document.body.classList.toggle('shell-drawer-open', isOpen);
    sidebar.inert = mobile.matches && !isOpen;
    sidebar.setAttribute('role', isOpen ? 'dialog' : 'complementary');
    if (isOpen) sidebar.setAttribute('aria-modal', 'true');
    else sidebar.removeAttribute('aria-modal');
    byId('shell-open-filters').setAttribute('aria-expanded', String(isOpen));
    document.querySelector('main').inert = isOpen;
    document.querySelector('header').inert = isOpen;
    if (isOpen) {
      drawerTrigger = document.activeElement;
      byId('shell-close-filters').focus();
    } else if (drawerTrigger) {
      drawerTrigger.focus();
      drawerTrigger = null;
    }
  }

  function closeManagement() {
    const menu = byId('shell-management');
    if (menu) menu.open = false;
  }

  function setupHeader() {
    const header = document.querySelector('body > header');
    header.classList.add('shell-header');
    const brand = header.querySelector('.brand');
    const path = brand.querySelector('.path-badge');
    brand.innerHTML = `<div class="shell-logo">${icon('gallery')}</div><div class="shell-brand-copy"><div class="shell-brand-title">Twitter / Pixiv</div><div class="shell-brand-subtitle">智慧標籤圖庫</div></div>`;
    if (path) brand.querySelector('.shell-brand-subtitle').append(path);

    const actions = header.querySelector('.header-actions');
    const ai = actions.querySelector('[onclick="openAiModal()"]');
    if (ai) ai.removeAttribute('style');
    labelButton(ai, 'spark', '智慧標籤', 'shell-ai');
    const settings = byId('btn-settings');
    labelButton(settings, 'settings', '設定', 'shell-settings');
    const management = document.createElement('details');
    management.id = 'shell-management';
    management.className = 'shell-management';
    management.innerHTML = `<summary class="btn" aria-label="管理工具">${icon('tools')}<span class="shell-action-label">管理工具</span><span class="shell-chevron">${icon('chevron')}</span></summary><div class="shell-management-panel" aria-label="圖庫管理工具"></div>`;
    const panel = management.querySelector('.shell-management-panel');
    const toolLabels = [
      ['openRenameModal()', '批次改名與排序'],
      ['openOrganizeModal()', '資料夾歸檔精靈'],
      ["openSettings('history')", '系統操作與處理日誌'],
      ['restoreLastDeletedImages()', '復原上次刪除'],
      ['rescanLibrary()', '重新掃描圖庫'],
      ['unloadAiModel(false)', '卸載模型・釋放記憶體'],
    ];
    for (const [handler, text] of toolLabels) {
      const button = actions.querySelector(`[onclick="${handler}"]`);
      if (!button) continue;
      button.textContent = text;
      panel.append(button);
      button.addEventListener('click', closeManagement);
    }
    actions.insertBefore(management, settings || null);
    management.querySelector('summary').addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        management.open = true;
        panel.querySelector('button')?.focus();
      }
    });
    document.addEventListener('click', event => {
      if (!management.contains(event.target)) closeManagement();
    });
  }

  function setupSidebar() {
    const sidebar = document.querySelector('aside');
    sidebar.classList.add('shell-sidebar');
    sidebar.id = 'shell-sidebar';
    sidebar.setAttribute('aria-label', '圖庫導覽與篩選');
    const stats = document.querySelector('.stats-bar');
    stats.className = 'shell-nav';
    stats.setAttribute('aria-label', '收藏範圍');
    const navDefinitions = [
      ['all', 'grid', '全部圖片'], ['root', 'inbox', '待整理'],
      ['folders', 'folder', '已歸檔'], ['ai', 'check', 'AI 已標記'],
      ['suggested', 'spark', '建議歸檔'],
    ];
    [...stats.querySelectorAll('.stat-pill')].forEach((pill, index) => {
      const [scope, symbol, label] = navDefinitions[index];
      const count = pill.querySelector('b');
      pill.innerHTML = `${icon(symbol)}<span>${label}</span>`;
      if (count) pill.append(count);
      pill.dataset.scope = scope;
      pill.setAttribute('role', 'button');
      pill.tabIndex = 0;
      activateWithKeyboard(pill);
      pill.addEventListener('click', () => {
        // Collection navigation changes scope; content tags remain available.
        if (scope !== 'ai') state.aiStatus = 'all';
        else {
          state.folder = '__ALL__';
          renderFolderList();
          renderActiveFilters();
        }
      }, true);
    });
    stats.insertAdjacentHTML('afterbegin', '<div class="shell-nav-heading">依圖庫整理狀態篩選</div>');
    setupSidebarTabs(sidebar, stats);
    sidebar.insertAdjacentHTML('afterbegin', `<div class="shell-drawer-close"><span>收藏與篩選</span><button class="btn" id="shell-close-filters" aria-label="關閉篩選">${icon('close')}</button></div>`);
    sidebar.insertAdjacentHTML('beforeend', '<button class="btn btn-primary shell-drawer-apply" id="shell-apply-filters">查看圖片結果</button>');
    byId('shell-close-filters').addEventListener('click', () => setDrawer(false));
    byId('shell-apply-filters').addEventListener('click', () => setDrawer(false));
    document.body.insertAdjacentHTML('beforeend', '<button class="shell-drawer-backdrop" tabindex="-1" aria-label="關閉篩選面板"></button>');
    document.querySelector('.shell-drawer-backdrop').addEventListener('click', () => setDrawer(false));
    byId('tag-search-input').placeholder = '搜尋畫師、角色或標籤…';
    byId('tag-search-input').setAttribute('aria-label', '搜尋標籤與畫師');
    for (const tab of sidebar.querySelectorAll('.cat-pill')) {
      if (tab.tagName !== 'BUTTON') {
        tab.setAttribute('role', 'button');
        tab.tabIndex = 0;
        activateWithKeyboard(tab);
      }
    }
    byId('tab-btn-tags').textContent = '標籤與畫師';
    mobile.addEventListener('change', () => setDrawer(false));
  }

  function setupSidebarTabs(sidebar, stats) {
    const tabs = sidebar.querySelector('.sidebar-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', '圖庫篩選分類');
    const libraryPanel = document.createElement('section');
    libraryPanel.id = 'panel-library';
    libraryPanel.className = 'sidebar-section shell-library-panel';
    libraryPanel.append(stats);
    tabs.after(libraryPanel);
    const definitions = [
      ['tags', '標籤與畫師'], ['folders', '資料夾'], ['library', '圖庫篩選'],
    ];
    const buttons = new Map();
    for (const [key, label] of definitions) {
      const previous = byId(`tab-btn-${key}`);
      const button = document.createElement('button');
      button.id = `tab-btn-${key}`;
      button.type = 'button';
      button.className = 'sidebar-tab';
      button.textContent = label;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `panel-${key}`);
      button.addEventListener('click', () => window.switchSidebarTab(key));
      if (previous) previous.replaceWith(button);
      else tabs.append(button);
      const panel = byId(`panel-${key}`);
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', button.id);
      buttons.set(key, button);
    }
    GalleryApp.use('switchSidebarTab', function (next, key) {
      const selected = buttons.has(key) ? key : 'tags';
      next(selected);
      for (const [name, button] of buttons) {
        const active = name === selected;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
        button.tabIndex = active ? 0 : -1;
        const panel = byId(`panel-${name}`);
        panel.hidden = !active;
        panel.style.display = active ? 'flex' : 'none';
      }
    });
    tabs.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !event.target.matches('[role="tab"]')) return;
      event.preventDefault();
      event.stopPropagation();
      const keys = [...buttons.keys()];
      const current = keys.findIndex(key => buttons.get(key) === event.target);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length;
      window.switchSidebarTab(keys[next]);
      buttons.get(keys[next]).focus();
    });
    window.switchSidebarTab('tags');
  }

  function setupBrowsing() {
    const main = document.querySelector('main');
    main.insertAdjacentHTML('afterbegin', `<div class="shell-library-heading"><div class="shell-library-title"><button class="btn shell-sidebar-toggle" id="shell-open-filters" aria-controls="shell-sidebar" aria-expanded="false" aria-label="開啟收藏與篩選">${icon('filter')}</button><div class="shell-title-copy"><div class="shell-eyebrow">YOUR COLLECTION</div><h1 id="shell-library-title">全部圖片</h1><div class="shell-result-copy"><span id="shell-result-count">正在讀取圖庫…</span><span class="shell-load-status" id="shell-load-status" role="status" hidden>更新中…</span></div></div></div><div class="shell-view-controls"><span class="shell-view-label">顯示方式</span><div class="shell-density" role="group" aria-label="圖片顯示方式"><button data-density="comfortable" aria-label="顯示圖片與標籤" title="顯示圖片與標籤" aria-pressed="true">${icon('grid')}</button><button data-density="compact" aria-label="只顯示圖片" title="只顯示圖片" aria-pressed="false">${icon('compact')}</button></div></div></div>`);
    byId('shell-open-filters').addEventListener('click', () => setDrawer(true));
    document.querySelectorAll('[data-density]').forEach(button => {
      button.addEventListener('click', () => setDensity(button.dataset.density));
    });
    let density = 'comfortable';
    try { density = localStorage.getItem('gallery.shell.density'); } catch { /* Optional preference. */ }
    setDensity(density, false);

    const search = byId('main-search');
    const searchField = document.createElement('label');
    searchField.className = 'shell-search-field';
    searchField.innerHTML = `${icon('search')}<kbd class="shell-search-key" title="按 / 快速搜尋">/</kbd>`;
    search.before(searchField);
    searchField.insertBefore(search, searchField.lastElementChild);
    search.placeholder = '搜尋圖片、檔名、畫師或標籤…';
    search.setAttribute('aria-label', '搜尋圖庫');
    for (const [id, text] of [['sel-sort', '排序方式']]) {
      const select = byId(id);
      const label = document.createElement('label');
      label.className = 'shell-select-field';
      label.innerHTML = `<span>${text}</span>`;
      select.before(label);
      label.append(select);
      select.title = text;
    }
    const sort = byId('sel-sort');
    sort.options[0].textContent = '檔名・自然排序';
    sort.options[0].title = 'Windows 自然順序：_2 在 _10 前';
    const toolbarActions = document.querySelector('.toolbar-row > div:last-child');
    toolbarActions.classList.add('shell-toolbar-actions');
    toolbarActions.removeAttribute('style');
    labelButton(toolbarActions.querySelector('[onclick="selectAllCurrentPage()"]'), 'check', '全選本頁');
    labelButton(toolbarActions.querySelector('[onclick="clearAllFilters()"]'), 'reset', '重設篩選');
    byId('gallery-scroll').setAttribute('aria-label', '圖片瀏覽結果');
    setDrawer(false);
  }

  function setupFolderBrowser() {
    try {
      const saved = JSON.parse(localStorage.getItem('gallery.folders.preferences') || '{}');
      const names = value => Array.isArray(value) ? [...new Set(value.filter(name => typeof name === 'string' && name.length > 0 && name.length <= 1024))] : [];
      folderPreferences = { pinned: names(saved?.pinned).slice(0, 200), recent: names(saved?.recent).slice(0, 12),
        sort: ['name', 'count', 'updated'].includes(saved?.sort) ? saved.sort : 'name' };
    } catch { /* Invalid or unavailable storage leaves a usable empty view. */ }
    const search = byId('folder-search-input');
    search.placeholder = '搜尋資料夾名稱…';
    search.setAttribute('aria-label', '搜尋資料夾名稱');
    const searchField = document.createElement('div');
    searchField.className = 'folder-search-field';
    search.before(searchField);
    searchField.innerHTML = `${icon('search')}<button class="folder-search-clear" id="folder-clear-search" aria-label="清除資料夾搜尋" hidden>${icon('close')}</button>`;
    searchField.insertBefore(search, searchField.lastElementChild);
    searchField.insertAdjacentHTML('afterend', `<div class="folder-toolbar"><span id="folder-total" class="folder-note">讀取資料夾中…</span><label class="folder-sort-label"><span class="smart-visually-hidden">資料夾排序</span><select id="folder-sort" class="select-box" title="最近更新依資料夾內圖片的最新修改時間排序"><option value="name">名稱 A–Z</option><option value="count">圖片數 ↓</option><option value="updated">最近更新</option></select></label></div><p id="folder-preference-status" class="folder-note" role="status" hidden></p>`);
    byId('folder-clear-search').addEventListener('click', () => { search.value = ''; window.renderFolderList(); search.focus(); });
    byId('folder-sort').value = folderPreferences.sort;
    byId('folder-total').setAttribute('role', 'status');
    byId('folder-sort').addEventListener('change', event => {
      folderPreferences.sort = event.target.value;
      folderVisibleLimit = 100;
      saveFolderPreferences();
      window.renderFolderList();
    });
    GalleryApp.use('renderFolderList', (_next, ...args) => renderFolderBrowser(...args));
    GalleryApp.use('loadFolders', async function (next, ...args) {
      const previousFolders = state.folders;
      try {
        const result = await next(...args);
        foldersReady = true;
        folderLoadError = '';
        const available = new Set(state.folders.map(folder => folder.name));
        const oldPins = folderPreferences.pinned.length, oldRecent = folderPreferences.recent.length;
        folderPreferences.pinned = folderPreferences.pinned.filter(name => available.has(name));
        folderPreferences.recent = folderPreferences.recent.filter(name => available.has(name));
        if (oldPins !== folderPreferences.pinned.length || oldRecent !== folderPreferences.recent.length) saveFolderPreferences();
        const hasUpdated = state.folders.every(folder => Number.isFinite(folder.last_updated));
        byId('folder-sort').querySelector('[value="updated"]').disabled = !hasUpdated;
        byId('folder-sort').value = !hasUpdated && folderPreferences.sort === 'updated' ? 'name' : folderPreferences.sort;
        window.renderFolderList();
        return result;
      } catch (error) {
        state.folders = previousFolders;
        folderLoadError = '暫時無法讀取資料夾。';
        window.renderFolderList();
      }
    });
    GalleryApp.use('setFolderFilter', function (next, name) {
      if (state.folders.some(folder => folder.name === name)) {
        folderPreferences.recent = [name, ...folderPreferences.recent.filter(previous => previous !== name)].slice(0, 12);
        saveFolderPreferences();
      }
      return next(name);
    });
    window.renderFolderList();
  }

  function saveFolderPreferences() {
    try {
      localStorage.setItem('gallery.folders.preferences', JSON.stringify(folderPreferences));
      byId('folder-preference-status').hidden = true;
    } catch {
      byId('folder-preference-status').hidden = false;
      byId('folder-preference-status').textContent = '本次選擇已套用；瀏覽器無法儲存釘選與紀錄。';
    }
  }

  function renderFolderBrowser() {
    const box = byId('sidebar-folder-list');
    if (!box || !byId('folder-sort')) return;
    const normalize = value => String(value).normalize('NFKC').toLocaleLowerCase();
    const query = normalize(byId('folder-search-input').value.trim());
    const changedQuery = lastFolderQuery !== query;
    if (changedQuery) { folderVisibleLimit = 100; lastFolderQuery = query; }
    const scrollTop = changedQuery ? 0 : box.scrollTop;
    box.replaceChildren();
    byId('folder-clear-search').hidden = !query;
    byId('folder-total').textContent = foldersReady ? `${state.folders.length.toLocaleString()} 個資料夾` : '讀取資料夾中…';
    const message = (text, className = 'folder-empty-note') => {
      const item = document.createElement('p'); item.className = className; item.textContent = text; return item;
    };
    if (folderLoadError) {
      box.append(message(folderLoadError));
      const retry = document.createElement('button'); retry.className = 'btn'; retry.textContent = '重新讀取';
      retry.addEventListener('click', () => window.loadFolders()); box.append(retry); return;
    }
    if (!foldersReady) { box.append(message('正在讀取資料夾…')); return; }
    const lookup = new Map(state.folders.map(folder => [folder.name, folder]));
    const sortMode = byId('folder-sort').value;
    const sorted = folders => [...folders].sort((left, right) => {
      const primary = sortMode === 'count' ? right.count - left.count : sortMode === 'updated' ? (right.last_updated || 0) - (left.last_updated || 0) : 0;
      return primary || folderCollator.compare(left.name, right.name);
    });
    const group = (title, count) => {
      const section = document.createElement('section'); section.className = 'folder-group';
      const heading = document.createElement('h3'); heading.className = 'folder-group-heading';
      heading.append(document.createTextNode(title));
      const number = document.createElement('span'); number.textContent = String(count); heading.append(number);
      section.append(heading); return section;
    };
    const appendFolders = (container, folders, sectionName, paginate = false) => {
      const shown = paginate ? folders.slice(0, folderVisibleLimit) : folders;
      const fragment = document.createDocumentFragment();
      for (const folder of shown) {
        const row = document.createElement('div'); row.className = 'folder-browse-row'; row.dataset.folder = folder.name; row.dataset.section = sectionName;
        row.classList.toggle('active', state.folder === folder.name);
        const open = document.createElement('button'); open.type = 'button'; open.className = 'folder-open';
        open.setAttribute('aria-label', `查看資料夾：${folder.name}`); open.setAttribute('aria-pressed', String(state.folder === folder.name)); open.title = folder.name;
        open.innerHTML = icon('folder');
        const copy = document.createElement('span'); copy.className = 'folder-name-copy';
        const name = document.createElement('span'); name.className = 'folder-name'; name.textContent = folder.name; copy.append(name);
        if (sortMode === 'updated' && folder.last_updated > 0) {
          const date = document.createElement('small'); date.className = 'folder-updated';
          date.textContent = new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(folder.last_updated * 1000));
          date.title = '資料夾內圖片的最新修改時間'; copy.append(date);
        }
        const count = document.createElement('span'); count.className = 'folder-image-count'; count.textContent = Number(folder.count).toLocaleString();
        open.append(copy, count);
        open.addEventListener('click', () => { folderFocusRequest = { name: folder.name, section: sectionName, kind: 'open' }; window.setFolderFilter(folder.name); });
        const pinned = folderPreferences.pinned.includes(folder.name);
        const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'folder-pin'; pin.innerHTML = icon('star');
        pin.setAttribute('aria-pressed', String(pinned)); pin.setAttribute('aria-label', `${pinned ? '取消釘選' : '釘選'}：${folder.name}`); pin.title = pinned ? '取消釘選' : '釘選資料夾';
        pin.addEventListener('click', () => {
          folderFocusRequest = { name: folder.name, section: sectionName, kind: 'pin' };
          folderPreferences.pinned = pinned ? folderPreferences.pinned.filter(name => name !== folder.name) : [...folderPreferences.pinned, folder.name];
          saveFolderPreferences(); window.renderFolderList();
        });
        row.append(open, pin); fragment.append(row);
      }
      container.append(fragment);
      if (paginate && shown.length < folders.length) {
        const more = document.createElement('button'); more.className = 'btn folder-load-more'; more.textContent = `顯示更多（${shown.length} / ${folders.length}）`;
        more.addEventListener('click', () => { folderFocusRequest = { more: sectionName, from: folderVisibleLimit }; folderVisibleLimit += 100; window.renderFolderList(); }); container.append(more);
      }
    };
    if (!state.folders.length) box.append(message('目前沒有已歸檔的資料夾。可從「圖庫篩選」查看待整理圖片。'));
    else if (query) {
      const matches = sorted(state.folders.filter(folder => normalize(folder.name).includes(query)));
      byId('folder-total').textContent = `${matches.length.toLocaleString()} / ${state.folders.length.toLocaleString()} 符合`;
      const results = group('搜尋結果', matches.length); results.id = 'folder-search-results';
      appendFolders(results, matches, 'search', true);
      if (!matches.length) results.append(message('找不到符合的資料夾，試試名稱的一部分。'));
      box.append(results);
    } else {
      const pinnedNames = new Set(folderPreferences.pinned);
      const pinned = folderPreferences.pinned.map(name => lookup.get(name)).filter(Boolean);
      const recent = folderPreferences.recent.filter(name => !pinnedNames.has(name)).map(name => lookup.get(name)).filter(Boolean);
      const pins = group('釘選', pinned.length); pins.id = 'folder-pinned';
      appendFolders(pins, sorted(pinned), 'pinned');
      if (!pinned.length) pins.append(message('點選資料夾旁的星號，釘選常用位置。'));
      const history = group('最近使用', recent.length); history.id = 'folder-recent';
      appendFolders(history, recent, 'recent');
      if (!recent.length) history.append(message('開啟過的資料夾會顯示在這裡。'));
      const all = document.createElement('details'); all.id = 'folder-all'; all.className = 'folder-all'; all.open = folderAllExpanded;
      const summary = document.createElement('summary'); summary.textContent = `全部資料夾 · ${state.folders.length.toLocaleString()}`; all.append(summary);
      if (folderAllExpanded) appendFolders(all, sorted(state.folders), 'all', true);
      all.addEventListener('toggle', () => {
        if (!all.isConnected || all.open === folderAllExpanded) return;
        folderAllExpanded = all.open; folderVisibleLimit = 100; folderFocusRequest = { all: true }; window.renderFolderList();
      });
      box.append(pins, history, all);
    }
    box.scrollTop = scrollTop;
    if (folderFocusRequest) {
      const request = folderFocusRequest; folderFocusRequest = null;
      const rows = [...box.querySelectorAll('.folder-browse-row')].filter(row => row.dataset.folder === request.name);
      const row = rows.find(row => row.dataset.section === request.section) || rows[0];
      const target = request.all ? byId('folder-all')?.querySelector('summary') : request.more
        ? [...box.querySelectorAll('.folder-browse-row')].filter(row => row.dataset.section === request.more)[request.from]?.querySelector('.folder-open') || box.querySelector('.folder-load-more')
        : row?.querySelector(request.kind === 'pin' ? '.folder-pin' : '.folder-open');
      (target || byId('folder-search-input')).focus({ preventScroll: true });
    }
  }

  function setupSmartTagging() {
    const modal = byId('ai-modal');
    if (!modal) return;
    const dialog = modal.querySelector('.dialog-box');
    const pipeline = byId('ai-tab-pipeline');
    const ids = ['ai-scope', 'ai-limit', 'ai-step-local', 'ai-step-rev', 'ai-step-sem', 'ai-auto-move',
      'ai-gen-thresh', 'ai-art-thresh', 'rev-limit', 'rev-sim', 'rev-apikey', 'btn-start-smart-tag'];
    const controls = new Map(ids.map(id => [id, byId(id)]));
    let extras = byId('ai-step-decensor');
    while (extras.parentElement !== pipeline) extras = extras.parentElement;
    const tools = [...pipeline.querySelectorAll('[onclick="runPixivLookup()"], [onclick="purgeLowConfAiArtists()"]')];
    const live = byId('ai-modal-live-box');
    setupModelSettings();

    dialog.removeAttribute('style');
    dialog.classList.add('smart-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'smart-title');
    dialog.tabIndex = -1;
    dialog.innerHTML = `
      <div class="smart-header">
        <div class="smart-heading"><span class="smart-heading-icon">${icon('spark')}</span><div><h2 id="smart-title">智慧標籤</h2><p>選擇範圍與步驟，補齊標籤並整理圖片。</p></div></div>
        <div class="smart-header-actions"><button class="btn smart-model-link" id="smart-model-settings">${icon('settings')}<span>模型設定</span></button><button class="btn" id="smart-close" aria-label="關閉智慧標籤">${icon('close')}</button></div>
      </div>
      <div class="smart-content" id="ai-tab-pipeline">
        <div data-smart-slot="live"></div>
        <fieldset id="smart-options" class="smart-options">
          <legend class="smart-visually-hidden">智慧標籤處理選項</legend>
          <section class="smart-scope"><div class="smart-scope-main"><label for="ai-scope" class="smart-section-label">處理範圍</label><div data-smart-slot="ai-scope"></div><p id="smart-scope-note" class="smart-note"></p><p class="smart-note">自動標籤僅保留畫師、角色、作品與來源；人工標籤保留。</p></div><div class="smart-limit"><label for="ai-limit" class="smart-section-label">本機分析上限</label><div data-smart-slot="ai-limit"></div><p class="smart-note">張，0 表示不限</p></div></section>
          <section class="smart-steps-section"><div class="smart-section-heading"><h3>處理步驟</h3><span class="smart-note">依順序執行已勾選項目</span></div><div class="smart-steps">
            <label class="smart-step"><span class="smart-step-number">1</span><span class="smart-step-copy"><strong>AI 圖片標記</strong><small>辨識畫師、角色與作品。</small></span><span data-smart-slot="ai-step-local"></span></label>
            <label class="smart-step"><span class="smart-step-number">2</span><span class="smart-step-copy"><strong>搜圖校正</strong><small>比對圖片來源，補齊或校正畫師資訊。</small></span><span data-smart-slot="ai-step-rev"></span></label>
            <span hidden data-smart-slot="ai-step-sem"></span>
          </div></section>
          <label class="smart-archive"><span data-smart-slot="ai-auto-move"></span><span><strong>完成後依畫師歸檔</strong><small>會建立畫師資料夾，並將圖片搬入。</small></span></label>
          <details class="smart-advanced" id="smart-advanced"><summary>進階參數與工具<span class="smart-note">辨識門檻、搜圖設定</span></summary><div class="smart-advanced-body">
            <h4>AI 辨識</h4><div class="smart-fields"><span hidden data-smart-slot="ai-gen-thresh"></span><label>畫師辨識門檻<div data-smart-slot="ai-art-thresh"></div></label></div>
            <h4>搜圖校正</h4><div class="smart-fields"><label>單次校正張數<div data-smart-slot="rev-limit"></div></label><label>最低相似度（%）<div data-smart-slot="rev-sim"></div></label><label class="smart-field-wide">SauceNAO API Key <span class="smart-note">選填</span><div data-smart-slot="rev-apikey"></div></label></div>
            <details class="smart-extra"><summary>其他處理選項</summary><div id="smart-extra-options"></div></details>
            <div class="smart-maintenance"><h4>標籤維護</h4><p class="smart-note">手動查詢 Pixiv ID，或清理低信心度的畫師猜測。</p><div id="smart-maintenance-tools"></div></div>
          </div></details>
        </fieldset>
        <p id="smart-feedback" class="settings-feedback" role="status" aria-live="polite"></p>
      </div>
      <div class="smart-footer"><div><span id="smart-plan"></span><p class="smart-note" id="smart-start-note">執行時會自動載入所需模型。</p></div><div class="smart-footer-actions"><button class="btn" id="smart-close-footer">關閉</button><span data-smart-slot="btn-start-smart-tag"></span></div></div>`;
    for (const [id, control] of controls) {
      control.removeAttribute('style');
      dialog.querySelector(`[data-smart-slot="${id}"]`).replaceWith(control);
    }
    dialog.querySelector('[data-smart-slot="live"]').replaceWith(live);
    const liveWidth = byId('ai-modal-live-bar').style.width;
    live.querySelectorAll('[style]').forEach(element => element.removeAttribute('style'));
    live.removeAttribute('style');
    live.className = 'smart-live';
    live.hidden = true;
    live.firstElementChild.className = 'smart-live-heading';
    live.children[1].className = 'smart-progress-track';
    live.lastElementChild.className = 'smart-live-meta';
    byId('ai-modal-live-bar').style.width = liveWidth;
    byId('ai-modal-live-bar').parentElement.setAttribute('role', 'progressbar');
    byId('ai-modal-live-bar').parentElement.setAttribute('aria-label', '處理進度');
    byId('btn-modal-stop-task').textContent = '停止處理';
    extras.removeAttribute('style');
    byId('smart-extra-options').append(extras);
    for (const tool of tools) { tool.removeAttribute('style'); tool.classList.remove('btn-warn'); byId('smart-maintenance-tools').append(tool); }
    const scopeLabels = ['全圖庫待補齊的圖片（建議）', '僅根目錄待補齊的圖片', '未認證或低信心度畫師', '重新分析根目錄全部圖片'];
    [...byId('ai-scope').options].forEach((option, index) => { option.textContent = scopeLabels[index] || option.textContent; });
    byId('ai-limit').step = '1';
    byId('ai-gen-thresh').step = '0.01';
    byId('rev-apikey').type = 'password';
    byId('rev-apikey').autocomplete = 'off';
    for (const id of ['ai-limit', 'ai-gen-thresh', 'ai-art-thresh', 'rev-limit', 'rev-sim']) byId(id).required = true;
    byId('ai-scope').setAttribute('aria-describedby', 'smart-scope-note');
    byId('smart-options').addEventListener('change', updateSmartPlan);
    byId('smart-options').addEventListener('input', updateSmartPlan);
    byId('smart-close').addEventListener('click', () => window.closeAiModal());
    byId('smart-close-footer').addEventListener('click', () => window.closeAiModal());
    byId('smart-model-settings').addEventListener('click', () => {
      window.closeAiModal();
      window.openSettings('model');
      returnToSmartTag = true;
      byId('btn-close-settings').textContent = '返回智慧標籤';
    });
    byId('smart-model-settings').setAttribute('aria-label', '前往 AI 模型設定');
    GalleryApp.use('openAiModal', function (next, ...args) {
      if (!modal.classList.contains('open')) smartReturnFocus = document.activeElement;
      const result = next(...args);
      updateSmartPlan();
      byId('smart-close').focus();
      return result;
    });
    GalleryApp.use('closeAiModal', function (next, ...args) {
      const result = next(...args);
      if (smartReturnFocus?.isConnected) smartReturnFocus.focus();
      return result;
    });
    modal.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); window.closeAiModal(); }
      if (event.key !== 'Tab') return;
      const focusable = [...modal.querySelectorAll('button, input, select, summary, [tabindex="0"]')]
        .filter(element => !element.matches(':disabled') && element.tabIndex >= 0 && element.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    GalleryApp.use('startAiBatch', (_next, ...args) => startSmartTask(...args));
    GalleryApp.use('stopAiBatch', async function (next, ...args) {
      if (smartStopPending) return;
      smartStopPending = true;
      byId('btn-modal-stop-task').disabled = true;
      byId('btn-modal-stop-task').textContent = '停止中…';
      try { return await next(...args); }
      catch (error) {
        smartStopPending = false;
        byId('smart-feedback').textContent = `無法停止處理：${error.message}`;
        byId('smart-feedback').classList.add('error');
      }
    });
    updateSmartPlan();
  }

  function setupModelSettings() {
    const tabs = document.querySelector('.settings-tabs');
    tabs.insertAdjacentHTML('beforeend', '<button class="btn settings-tab" id="settings-tab-model" role="tab" aria-selected="false" aria-controls="settings-model" tabindex="-1" onclick="switchSettingsTab(\'model\')">AI 模型</button>');
    const panel = document.createElement('section');
    panel.id = 'settings-model';
    panel.className = 'settings-panel';
    panel.hidden = true;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', 'settings-tab-model');
    panel.innerHTML = `<div><h3>圖片標籤模型</h3><p class="settings-note">管理本機打標模型的記憶體與硬碟快取。智慧標籤會自動載入所需模型。</p></div>
      <div class="settings-section"><div class="shell-model-status"><span class="muted">目前狀態</span><span id="shell-model-state-slot"></span></div><div class="shell-model-device"><span>運算裝置</span><span id="shell-model-device">讀取中…</span></div><div class="settings-actions" id="shell-model-actions"></div><p class="settings-note" id="shell-model-busy" hidden>背景任務正在執行，完成後即可管理模型。</p></div>
      <div class="settings-section"><label class="settings-check"><span id="shell-model-auto-slot"></span><span>本機打標完成後自動卸載模型<span class="settings-note shell-model-help">釋放顯示卡記憶體，再繼續後續處理。下次任務會重新載入。</span></span></label></div>
      <details class="settings-section"><summary>硬碟快取 <span class="muted" id="shell-model-cache"></span></summary><p class="settings-note">刪除後，下次使用時需要重新下載模型。</p><div id="shell-model-cache-actions"></div></details><p id="shell-model-feedback" class="settings-feedback" role="status" aria-live="polite"></p>`;
    document.querySelector('.settings-content').append(panel);
    const modelState = byId('ai-modal-model-state');
    modelState.removeAttribute('style');
    byId('shell-model-state-slot').replaceWith(modelState);
    for (const [id, text] of [['btn-modal-load-vram', '載入模型'], ['btn-modal-unload-vram', '卸載模型'], ['btn-modal-unload-disk', '刪除模型快取']]) {
      const button = byId(id);
      button.removeAttribute('style');
      button.className = id === 'btn-modal-load-vram' ? 'btn btn-primary' : 'btn';
      button.textContent = text;
      button.disabled = true;
      (id === 'btn-modal-unload-disk' ? byId('shell-model-cache-actions') : byId('shell-model-actions')).append(button);
    }
    byId('btn-top-unload-model')?.remove();
    const autoUnload = byId('ai-auto-unload');
    byId('shell-model-auto-slot').replaceWith(autoUnload);
    try {
      const saved = localStorage.getItem('gallery.model.autoUnload');
      if (saved === 'true' || saved === 'false') autoUnload.checked = saved === 'true';
    } catch { /* The current checkbox still works without storage. */ }
    autoUnload.addEventListener('change', () => {
      try { localStorage.setItem('gallery.model.autoUnload', String(autoUnload.checked)); setModelFeedback('已記住此瀏覽器的選擇。'); }
      catch { setModelFeedback('本次選擇已套用；瀏覽器無法儲存此偏好。'); }
    });
    GalleryApp.use('switchSettingsTab', function (next, section) {
      const names = Array.from(tabs.querySelectorAll('[role="tab"]')).map(tab => tab.id.replace('settings-tab-', ''));
      const selected = names.includes(section) ? section : 'browse';
      next(selected === 'model' ? 'browse' : selected);
      for (const name of names) {
        const active = name === selected;
        byId(`settings-tab-${name}`).setAttribute('aria-selected', String(active));
        byId(`settings-tab-${name}`).tabIndex = active ? 0 : -1;
        byId(`settings-${name}`).hidden = !active;
      }
      byId(`settings-tab-${selected}`).scrollIntoView({ block: 'nearest', inline: 'nearest' });
      if (selected === 'model') { byId('settings-footer-note').textContent = '模型操作立即生效；自動卸載偏好由此瀏覽器記住。'; void window.checkAiStatus(); }
    });
    GalleryApp.use('handleSettingsTabKeydown', function (_next, event) {
      const names = Array.from(tabs.querySelectorAll('[role="tab"]')).map(tab => tab.id.replace('settings-tab-', ''));
      const current = names.findIndex(name => event.target.id === `settings-tab-${name}`);
      if (current < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? names.length - 1
        : (current + (event.key === 'ArrowLeft' ? -1 : 1) + names.length) % names.length;
      window.switchSettingsTab(names[next]);
      byId(`settings-tab-${names[next]}`).focus();
    });
    GalleryApp.use('closeScheduleModal', function (next, ...args) {
      const result = next(...args);
      if (returnToSmartTag) {
        returnToSmartTag = false;
        byId('btn-close-settings').textContent = '完成';
        window.openAiModal();
        byId('smart-model-settings').focus();
      }
      return result;
    });
    GalleryApp.use('syncModelUiState', (next, status) => { const result = next(status); syncSmartModel(status); return result; });
    GalleryApp.use('loadAiModelNow', () => runModelAction(false, false));
    GalleryApp.use('unloadAiModel', (_next, deleteDiskFiles = false) => runModelAction(true, deleteDiskFiles));
  }

  function updateSmartPlan() {
    if (!byId('smart-plan')) return;
    const count = ['ai-step-local', 'ai-step-rev', 'ai-step-sem', 'ai-step-decensor'].filter(id => byId(id)?.checked).length;
    byId('smart-plan').textContent = count ? `${count} 個處理步驟${byId('ai-auto-move').checked ? ' · 完成後歸檔' : ''}` : '請至少選擇一個處理步驟';
    const notes = {
      all_untagged: '包含子資料夾，補齊缺少的內容標籤與畫師資料。', root_untagged: '僅處理圖庫根目錄中資料尚未完整的圖片。',
      ai_low_conf: '聚焦於未取得可靠畫師資訊，或辨識信心度不足的圖片。', root_all: '重新處理根目錄所有圖片，包含已分析過的圖片。',
    };
    byId('smart-scope-note').textContent = notes[byId('ai-scope').value] || '';
    const busy = smartTaskBusy || latestModelStatus?.running;
    byId('btn-start-smart-tag').disabled = busy || modelActionPending || smartStartPending || !count;
    byId('btn-start-smart-tag').textContent = smartStartPending ? '啟動中…' : busy ? '背景任務處理中' : modelActionPending ? '模型處理中' : '開始處理';
    byId('smart-options').disabled = busy || modelActionPending || smartStartPending;
    byId('smart-start-note').textContent = busy ? '背景任務完成後，即可開始新的處理。' : '執行時會自動載入所需模型。';
  }

  function syncSmartModel(status) {
    if (status) latestModelStatus = status;
    if (!latestModelStatus || !byId('settings-model')) return;
    const busy = smartTaskBusy || latestModelStatus.running || latestModelStatus.lifecycle?.busy || modelActionPending;
    byId('ai-modal-model-state').textContent = modelActionPending ? '正在處理模型…' : latestModelStatus.model_loaded ? '已載入' : '未載入';
    byId('ai-modal-model-state').dataset.loaded = String(Boolean(latestModelStatus.model_loaded));
    const provider = latestModelStatus.provider || '';
    byId('shell-model-device').textContent = !latestModelStatus.model_loaded ? '未使用' : /Dml/i.test(provider) ? 'DirectML（GPU）' : /CUDA/i.test(provider) ? 'CUDA（GPU）' : /CPU/i.test(provider) ? 'CPU' : provider || 'GPU';
    byId('shell-model-cache').textContent = latestModelStatus.disk_cached ? `${latestModelStatus.disk_size_mb || 0} MB` : '無快取';
    byId('shell-model-busy').hidden = !smartTaskBusy && !latestModelStatus.running;
    byId('btn-modal-load-vram').disabled = busy || latestModelStatus.model_loaded;
    byId('btn-modal-unload-vram').disabled = busy || !latestModelStatus.model_loaded;
    byId('btn-modal-unload-disk').disabled = busy || (!latestModelStatus.model_loaded && !latestModelStatus.disk_cached);
    byId('btn-modal-load-vram').textContent = '載入模型';
    updateSmartPlan();
  }

  function updateSmartTask(status, running) {
    if (!byId('smart-options')) return;
    smartTaskBusy = Boolean(running || status?.running);
    const completed = !smartTaskBusy && status?.processed_count === 100;
    if (smartTaskBusy) smartTaskCompleted = false;
    else if (completed) smartTaskCompleted = true;
    const live = byId('ai-modal-live-box');
    live.hidden = !smartTaskBusy && !smartTaskCompleted && !smartStartPending;
    const scheduled = (status?.status_message || '').startsWith('正在執行排程自動處理');
    byId('btn-modal-stop-task').hidden = !smartTaskBusy || scheduled || status?.can_stop === false;
    if (!smartTaskBusy) smartStopPending = false;
    byId('btn-modal-stop-task').disabled = smartStopPending;
    byId('btn-modal-stop-task').textContent = smartStopPending ? '停止中…' : '停止處理';
    live.lastElementChild.hidden = scheduled;
    if (scheduled) byId('ai-modal-live-msg').textContent = '排程自動處理中';
    const progress = byId('ai-modal-live-bar').parentElement;
    progress.setAttribute('aria-valuemin', '0'); progress.setAttribute('aria-valuemax', '100');
    const percent = smartTaskBusy ? Math.min(100, Math.max(0, Math.round((status.processed_count || 0) / (status.total_target > 0 ? status.total_target : 100) * 100)))
      : completed ? 100 : Math.round(parseFloat(byId('ai-modal-live-bar').style.width) || 0);
    byId('ai-modal-live-bar').style.width = `${percent}%`;
    progress.setAttribute('aria-valuenow', String(percent));
    byId('ai-modal-live-pct').textContent = `${percent}%`;
    updateSmartPlan();
    syncSmartModel();
  }

  function setModelFeedback(message, error = false) {
    byId('shell-model-feedback').textContent = message;
    byId('shell-model-feedback').classList.toggle('error', error);
  }

  async function smartRequest(url) {
    const response = await fetch(url, { method: 'POST' });
    const data = await response.json();
    if (!response.ok || ['error', 'failed', 'busy', 'already_running'].includes(data.status)) {
      throw new Error(typeof data.detail === 'string' ? data.detail : data.message || '操作失敗，請稍後重試。');
    }
    return data;
  }

  async function runModelAction(unload, deleteDiskFiles) {
    if (modelActionPending || smartTaskBusy || latestModelStatus?.running) return;
    if (deleteDiskFiles && !confirm('確定要卸載模型並刪除硬碟快取嗎？下次使用時需要重新下載。')) return;
    modelActionPending = true;
    setModelFeedback(unload ? '正在卸載模型…' : '正在載入模型…');
    syncSmartModel();
    try {
      const data = await smartRequest(unload ? `/api/ai/unload?delete_disk_files=${deleteDiskFiles}` : '/api/ai/load');
      setModelFeedback(data.message || (unload ? '模型已卸載。' : '模型已載入。'));
      await window.checkAiStatus();
    } catch (error) { setModelFeedback(error.message, true); }
    finally { modelActionPending = false; syncSmartModel(); }
  }

  async function startSmartTask() {
    if (smartTaskBusy || smartStartPending || modelActionPending || latestModelStatus?.running) return;
    const steps = { run_local_ai: byId('ai-step-local').checked, run_reverse_search_calibrate: byId('ai-step-rev').checked, run_semantic_themes: false, run_decensor: byId('ai-step-decensor').checked };
    const feedback = byId('smart-feedback');
    feedback.classList.remove('error');
    if (!Object.values(steps).some(Boolean)) { feedback.textContent = '請至少選擇一個處理步驟。'; return; }
    const validate = [...(steps.run_local_ai ? ['ai-limit', 'ai-art-thresh'] : []), ...(steps.run_reverse_search_calibrate ? ['rev-limit', 'rev-sim'] : [])];
    for (const id of validate) {
      const input = byId(id);
      if (input.checkValidity()) continue;
      for (let parent = input.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
      input.reportValidity(); input.focus(); return;
    }
    const params = new URLSearchParams({ scope: byId('ai-scope').value, limit: byId('ai-limit').value, general_thresh: byId('ai-gen-thresh').value,
      artist_thresh: byId('ai-art-thresh').value, ...steps, reverse_search_limit: byId('rev-limit').value, min_similarity: byId('rev-sim').value,
      auto_move_to_artist_folder: byId('ai-auto-move').checked, auto_unload: byId('ai-auto-unload').checked, decensor_save_mode: byId('ai-decensor-save-mode').value });
    if (byId('rev-apikey').value.trim()) params.set('saucenao_api_key', byId('rev-apikey').value.trim());
    smartStartPending = true;
    smartTaskCompleted = false;
    byId('ai-modal-live-box').hidden = true;
    feedback.textContent = '正在啟動處理…';
    updateSmartPlan();
    try {
      await smartRequest(`/api/ai/start?${params}`);
      feedback.textContent = '已啟動，處理進度會持續更新。';
      await window.checkAiStatus();
    } catch (error) { feedback.textContent = error.message; feedback.classList.add('error'); }
    finally { smartStartPending = false; updateSmartPlan(); }
  }

  function syncShell() {
    if (typeof state === 'undefined' || !byId('shell-library-title')) return;
    const scopeTitles = { __ALL__: '全部圖片', __ROOT__: '待整理', __FOLDERS__: '已歸檔', __SUGGESTED__: '建議歸檔' };
    const aiOnly = state.aiStatus === 'tagged';
    byId('shell-library-title').textContent = state.folder === '__ALL__' && aiOnly ? 'AI 已標記' : scopeTitles[state.folder] || state.folder;
    byId('shell-library-title').title = byId('shell-library-title').textContent;
    byId('shell-result-count').textContent = `${state.total.toLocaleString()} 張圖片${state.includeTags.length || byId('main-search').value.trim() ? '・已套用篩選' : ''}`;
    const scopes = { all: '__ALL__', root: '__ROOT__', folders: '__FOLDERS__', suggested: '__SUGGESTED__' };
    document.querySelectorAll('.shell-nav .stat-pill').forEach(pill => {
      const active = pill.dataset.scope === 'ai' ? aiOnly : state.folder === scopes[pill.dataset.scope] && (pill.dataset.scope !== 'all' || !aiOnly);
      pill.setAttribute('aria-pressed', String(active));
    });
    byId('tab-btn-folders').textContent = `資料夾 (${state.folders.length})`;
    const summary = document.querySelector('.filter-summary');
    if (summary) summary.hidden = !byId('active-filters-bar').querySelector('.filter-chip');
    document.querySelectorAll('.cat-pill').forEach(pill => pill.setAttribute('aria-pressed', String(pill.classList.contains('active'))));
    document.querySelectorAll('.sidebar-tab').forEach(tab => tab.setAttribute('aria-selected', String(tab.classList.contains('active'))));
  }

  function enhanceCards() {
    const grid = byId('image-grid');
    grid.querySelectorAll('.card').forEach(card => {
      const thumbnail = card.querySelector('.thumb-box');
      const check = card.querySelector('.card-check');
      const image = thumbnail?.querySelector('img');
      if (thumbnail && !thumbnail.dataset.shellReady) {
        thumbnail.dataset.shellReady = 'true';
        thumbnail.setAttribute('role', 'button');
        thumbnail.setAttribute('aria-label', `查看圖片：${image?.alt || ''}`);
        thumbnail.tabIndex = 0;
        thumbnail.addEventListener('keydown', event => {
          if (event.target !== thumbnail || !['Enter', ' '].includes(event.key)) return;
          event.preventDefault(); thumbnail.click();
        });
      }
      if (check && !check.dataset.shellReady) {
        check.dataset.shellReady = 'true';
        check.setAttribute('role', 'checkbox');
        check.setAttribute('aria-checked', String(card.classList.contains('selected')));
        check.setAttribute('aria-label', `選取 ${image?.alt || '圖片'}`);
        check.tabIndex = 0;
      }
      const time = card.querySelector('.card-tags > .pill[title*="時間"]');
      if (time) {
        time.className = 'shell-card-time';
        time.removeAttribute('style');
        time.textContent = time.textContent.replace(/^\s*🕒\s*/, '');
        time.parentElement.append(time);
      }
    });
    if (!state.items.length && !grid.querySelector('.shell-empty')) {
      grid.innerHTML = `<div class="shell-empty">${icon('search')}<h2>沒有符合條件的圖片</h2><p>試試其他關鍵字，或移除部分篩選條件。</p><button class="btn" id="shell-reset-empty">重設篩選</button></div>`;
      byId('shell-reset-empty').addEventListener('click', () => clearAllFilters());
    }
  }

  function after(name, callback) {
    GalleryApp.use(name, (next, ...args) => {
      const result = next(...args);
      if (result?.then) return result.then(value => { callback(); return value; });
      callback();
      return result;
    });
  }

  function installHooks() {
    ['renderActiveFilters', 'renderPagination', 'renderFolderList', 'switchSidebarTab', 'setTagCategory'].forEach(name => after(name, syncShell));
    after('loadStats', () => syncSmartModel(state.stats.ai_status));
    GalleryApp.use('renderGrid', function (next, ...args) {
      const focused = document.activeElement;
      const cardId = focused.closest('.card')?.id;
      const target = focused.matches('.card-check') ? '.card-check' : '.thumb-box';
      const result = next(...args);
      enhanceCards();
      if (cardId) byId(cardId)?.querySelector(target)?.focus({ preventScroll: true });
      return result;
    });
    GalleryApp.use('updateAiBanner', function (next, status, running) {
      // Stats snapshots describe the model only; they must not create a task
      // or overwrite a running reverse-search / scheduled-import banner.
      if (running === undefined && !status?.running) {
        if (typeof wasAiRunning === 'undefined' || !wasAiRunning) { byId('ai-banner').classList.remove('active'); updateSmartTask(status, false); }
        return;
      }
      const result = next(status, running ?? Boolean(status?.running));
      const pause = byId('ai-banner').querySelector('button');
      if (pause) pause.hidden = !status?.running || (!status.can_stop && (status.external_process || (status.status_message || '').startsWith('正在執行排程自動處理')));
      updateSmartTask(status, running ?? Boolean(status?.running));
      return result;
    });
    GalleryApp.use('loadImages', async function (next, ...args) {
      const sequence = ++loadingSequence;
      byId('gallery-scroll').setAttribute('aria-busy', 'true');
      byId('shell-load-status').hidden = false;
      syncShell();
      try {
        const result = await next(...args);
        if (sequence === loadingSequence && !args[0]?.preserveOnError && byId('page-info').textContent.startsWith('圖片載入失敗')) showLoadError();
        return result;
      } catch (error) {
        if (sequence !== loadingSequence) return;
        if (args[0]?.preserveOnError) return false;
        showLoadError();
        console.warn('Gallery images could not be loaded:', error);
      } finally {
        if (sequence === loadingSequence) {
          byId('gallery-scroll').setAttribute('aria-busy', 'false');
          byId('shell-load-status').hidden = true;
        }
      }
    });
  }

  function showLoadError() {
    byId('image-grid').innerHTML = `<div class="shell-empty">${icon('inbox')}<h2>暫時無法載入圖片</h2><p>請確認圖庫服務連線，再試一次。</p><button class="btn" id="shell-retry">重新載入</button></div>`;
    byId('shell-result-count').textContent = '連線失敗';
    byId('shell-retry').addEventListener('click', () => window.loadImages());
  }

  function handleKeydown(event) {
    if (drawerOpen) {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation(); setDrawer(false);
      } else if (event.key === 'Tab') {
        const focusable = [...document.querySelector('.shell-sidebar').querySelectorAll('button, input, [tabindex="0"]')]
          .filter(element => !element.disabled && element.getClientRects().length);
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      } else if (['ArrowLeft', 'ArrowRight'].includes(event.key) && !event.target.closest('input, select, textarea, .sidebar-tabs')) {
        event.stopImmediatePropagation();
      }
      return;
    }
    const menu = byId('shell-management');
    if (event.key === 'Escape' && menu.open) {
      event.preventDefault(); event.stopImmediatePropagation(); closeManagement(); menu.querySelector('summary').focus();
    }
    if (menu.open && ['ArrowLeft', 'ArrowRight'].includes(event.key)) event.stopImmediatePropagation();
    if (event.key === '/' && !event.ctrlKey && !event.altKey && !event.metaKey && !event.isComposing &&
        !event.target.closest('input, textarea, select, [contenteditable="true"]') && !document.querySelector('.modal-backdrop.open')) {
      event.preventDefault(); byId('main-search').focus();
    }
  }

  function initialize() {
    if (!document.querySelector('body > header .stats-bar') || !byId('main-search')) return;
    setupHeader();
    setupSidebar();
    setupBrowsing();
    setupFolderBrowser();
    setupSmartTagging();
    installHooks();
    syncShell();
    window.addEventListener('keydown', handleKeydown, true);
  }
  // A deferred script runs before the page's DOMContentLoaded data requests.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true });
  else initialize();
})();
