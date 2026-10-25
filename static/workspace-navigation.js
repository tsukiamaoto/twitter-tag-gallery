/* Workspace navigation keeps the gallery and each editor mounted exactly once. */
(() => {
  'use strict';
  const app = window.GalleryApp, byId = id => document.getElementById(id);
  const paths = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"/>',
    schedule: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-9 3v3l2 1"/>',
    ai: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
    model: '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4M9 9h6v6H9Z"/>',
    mcp: '<path d="m8 3-5 5 5 5m8-2 5 5-5 5M7 17 17 7"/>',
    tools: '<path d="M14 6a5 5 0 0 0-6 6L3 17a3 3 0 0 0 4 4l5-5a5 5 0 0 0 6-6l-3 3-4-4Z"/>',
    skills: '<path d="M5 3h11l3 3v15H5ZM9 8h6M9 12h6M9 16h4"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  };
  const icon = name => `<svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
  const definitions = [
    ['model', 'AI 模型', '模型、下載與記憶體'],
    ['mcp', 'MCP 管理', '客戶端連線與存取模式'],
    ['tools', 'Tool 設定', '工具列表、畫師身分與清除重複圖片'],
    ['image-skills', 'Skill 管理', 'Agent 技能與指定模型'],
  ];
  const aliases = new Set(['model', 'mcp', 'tools', 'artists', 'dedup', 'tool-catalog', 'skills', 'image-skills']);

  function initialize() {
    if (app.runtime.navigation || !byId('settings-model')) return;
    let workspace = 'home', section = 'model', toolPage = 'catalog', smartOrigin = null;
    let homeScroll = [], pageScroll = new Map();
    const layout = document.querySelector('.layout'), main = document.querySelector('main');
    const sidebar = byId('shell-sidebar');
    const home = document.createElement('div'); home.id = 'home-workspace'; home.className = 'workspace-view workspace-home';
    home.append(...main.childNodes); main.append(home);
    const homeSidebar = document.createElement('div'); homeSidebar.id = 'home-sidebar'; homeSidebar.className = 'workspace-sidebar workspace-home-sidebar';
    homeSidebar.append(...[...sidebar.childNodes].filter(node => node !== byId('shell-apply-filters') && !node.classList?.contains('shell-drawer-close')));
    sidebar.querySelector('.shell-drawer-close').after(homeSidebar);

    const rail = document.createElement('nav'); rail.id = 'workspace-rail'; rail.className = 'workspace-rail'; rail.setAttribute('aria-label', '工作區');
    rail.innerHTML = `<button type="button" id="workspace-home" class="workspace-rail-button" aria-label="首頁" title="首頁">${icon('home')}<span>首頁</span></button><button type="button" id="workspace-schedule" class="workspace-rail-button" aria-label="匯入排程" title="匯入排程">${icon('schedule')}<span>排程</span></button><button type="button" id="workspace-ai" class="workspace-rail-button" aria-label="AI 設定" title="AI 設定">${icon('ai')}<span>AI</span></button>`;
    layout.prepend(rail);
    const aiSidebar = document.createElement('div'); aiSidebar.id = 'ai-sidebar'; aiSidebar.className = 'workspace-sidebar ai-sidebar'; aiSidebar.hidden = true;
    aiSidebar.innerHTML = '<div class="ai-sidebar-heading"><h2>AI 設定</h2><p>模型、連線與工作流程</p></div><div id="ai-category-list" role="tablist" aria-label="AI 設定分類" aria-orientation="vertical"></div>';
    homeSidebar.after(aiSidebar);
    const ai = document.createElement('section'); ai.id = 'ai-workspace'; ai.className = 'workspace-view ai-workspace'; ai.hidden = true;
    ai.setAttribute('aria-labelledby', 'ai-page-title');
    ai.innerHTML = `<div class="ai-page-heading"><div class="ai-page-title"><button type="button" class="btn shell-sidebar-toggle" id="ai-open-navigation" aria-controls="shell-sidebar" aria-expanded="false" aria-label="開啟 AI 功能選單">${icon('menu')}</button><div><p>AI 設定</p><h1 id="ai-page-title" tabindex="-1">AI 模型</h1></div></div><button type="button" class="btn" id="ai-return-smart" hidden>返回智慧標籤</button></div><div class="ai-settings-content" id="ai-settings-content"></div><div class="ai-settings-footer" id="ai-settings-footer" hidden></div>`;
    main.append(ai);
    const schedule = document.createElement('section'); schedule.id = 'schedule-workspace'; schedule.className = 'workspace-view schedule-workspace'; schedule.hidden = true;
    schedule.setAttribute('aria-labelledby', 'schedule-page-title');
    schedule.innerHTML = `<div class="schedule-page-heading"><div><p class="schedule-eyebrow">圖庫自動化</p><h1 id="schedule-page-title" tabindex="-1">匯入排程</h1><p class="schedule-page-description">設定好時間，讓新圖片自動入庫、打標與歸檔。</p></div><span class="schedule-state" id="schedule-state" role="status" data-state="loading"><span class="schedule-dot" aria-hidden="true"></span><span>讀取中</span></span></div><div class="schedule-content" id="schedule-content"></div><div class="schedule-footer"><div class="schedule-footer-inner"><div class="schedule-footer-copy"><p id="schedule-draft-status">正在讀取設定…</p><p id="schedule-feedback" class="settings-feedback" role="status"></p><button type="button" class="btn btn-text" id="schedule-retry" hidden>重新載入設定</button></div></div></div>`;
    // Assemble offscreen before appending; keep every input mounted exactly once.
    schedule.querySelector('#schedule-content').append(byId('settings-schedule'));
    schedule.querySelector('#settings-schedule').hidden = false;
    schedule.querySelector('.schedule-footer-inner').append(byId('schedule-footer-actions'));
    schedule.querySelector('#schedule-footer-actions').hidden = false;
    main.append(schedule);
    const content = byId('ai-settings-content');
    const toolsGroup = document.createElement('section'); toolsGroup.id = 'ai-tools-group'; toolsGroup.setAttribute('role', 'tabpanel'); toolsGroup.setAttribute('aria-labelledby', 'settings-tab-tools');
    toolsGroup.innerHTML = '<div class="ai-tool-tabs" role="tablist" aria-label="Tool 設定分類"><button type="button" class="btn settings-tab" id="settings-tab-tool-catalog" role="tab" aria-controls="settings-tools">工具管理</button></div>';
    content.append(toolsGroup);
    for (const [key, label, help] of definitions) {
      const button = byId(`settings-tab-${key}`);
      button.className = 'workspace-category'; button.innerHTML = `${icon(key === 'image-skills' ? 'skills' : key)}<span><strong>${label}</strong><small>${help}</small></span>`;
      button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', key === 'tools' ? 'ai-tools-group' : `settings-${key}`);
      byId('ai-category-list').append(button);
      if (key !== 'tools') content.append(byId(`settings-${key}`));
    }
    const artistTab = byId('settings-tab-artists'); artistTab.className = 'btn settings-tab';
    const toolTabsBar = toolsGroup.querySelector('.ai-tool-tabs');
    toolTabsBar.hidden = true;
    toolTabsBar.append(artistTab);
    const dedupTab = byId('settings-tab-dedup'), dedupPanel = byId('settings-dedup');
    if (dedupTab) {
      dedupTab.className = 'btn settings-tab';
      toolTabsBar.append(dedupTab);
    }
    toolsGroup.append(byId('settings-tools'));
    const promptManagement = byId('settings-mcp').querySelector('.agent-management');
    if (promptManagement) byId('settings-tools').append(promptManagement);
    const toolsContainer = byId('settings-tools').querySelector('.agent-management') || byId('settings-tools');
    const toolsToolbar = toolsContainer.querySelector('.agent-tool-toolbar');
    const artistDetails = document.createElement('details');
    artistDetails.className = 'settings-section';
    artistDetails.id = 'artist-workflow-details';
    artistDetails.innerHTML = '<summary>畫師跨平台身分（待補查佇列與 Antigravity 指令）</summary>';
    byId('settings-artists').hidden = false;
    artistDetails.append(byId('settings-artists'));
    const dedupDetails = document.createElement('details');
    dedupDetails.className = 'settings-section';
    dedupDetails.id = 'dedup-workflow-details';
    dedupDetails.innerHTML = '<summary>完全重複圖片清理（快速預覽與 Antigravity 指令）</summary>';
    if (dedupPanel) {
      dedupPanel.hidden = false;
      dedupDetails.append(dedupPanel);
    }
    if (toolsToolbar) {
      toolsToolbar.before(artistDetails, dedupDetails);
    } else {
      toolsContainer.prepend(artistDetails, dedupDetails);
    }
    const agentJobs = byId('agent-jobs')?.closest('section');
    if (agentJobs) byId('settings-tasks').append(agentJobs);
    byId('settings-tools').setAttribute('aria-labelledby', 'settings-tab-tool-catalog');

    const pageKey = () => section === 'tools' ? `tools-${toolPage}` : section;
    function navigate(nextWorkspace, nextSection = section, nextToolPage = toolPage) {
      const previousWorkspace = workspace;
      if (workspace === 'home' && nextWorkspace !== 'home') {
        window.hideHoverPreview();
        homeScroll = ['gallery-scroll', 'sidebar-tag-list', 'sidebar-folder-list', 'panel-library'].map(id => [byId(id), byId(id)?.scrollTop || 0]);
      }
      if (workspace === 'ai') pageScroll.set(pageKey(), content.scrollTop);
      if (workspace === 'schedule') pageScroll.set('schedule', byId('schedule-content').scrollTop);
      app.runtime.setDrawer(false);
      if (byId('schedule-modal').classList.contains('open')) window.closeScheduleModal();
      window.closeTimePicker();
      workspace = nextWorkspace; section = nextSection; toolPage = nextToolPage;
      const isAi = workspace === 'ai';
      document.body.dataset.workspace = workspace;
      home.hidden = workspace !== 'home'; home.inert = workspace !== 'home'; homeSidebar.hidden = workspace !== 'home';
      ai.hidden = !isAi; aiSidebar.hidden = !isAi;
      schedule.hidden = workspace !== 'schedule';
      sidebar.setAttribute('aria-label', isAi ? 'AI 設定導覽' : '圖庫導覽與篩選');
      sidebar.querySelector('.shell-drawer-close > span').textContent = isAi ? 'AI 設定' : '收藏與篩選';
      byId('shell-apply-filters').hidden = isAi;
      for (const key of ['home', 'schedule', 'ai']) {
        const button = byId(`workspace-${key}`);
        if (key === workspace) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
      }
      for (const [key] of definitions) {
        const active = key === section;
        const button = byId(`settings-tab-${key}`);
        button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
        if (key !== 'tools') byId(`settings-${key}`).hidden = !active;
      }
      toolsGroup.hidden = section !== 'tools';
      byId('settings-tools').hidden = section !== 'tools';
      for (const [key, id] of [['catalog', 'tool-catalog'], ['artists', 'artists'], ['dedup', 'dedup']]) {
        const active = key === toolPage;
        const tabNode = byId(`settings-tab-${id}`);
        if (tabNode) {
          tabNode.setAttribute('aria-selected', String(active));
          tabNode.tabIndex = active ? 0 : -1;
        }
      }
      if (section === 'tools') {
        if (byId(' settings-artists')) byId('settings-artists').hidden = false;
        if (byId('settings-artists')) byId('settings-artists').hidden = false;
        if (byId('settings-dedup')) byId('settings-dedup').hidden = false;
        if (toolPage === 'artists' && byId('artist-workflow-details')) byId('artist-workflow-details').open = true;
        if (toolPage === 'dedup' && byId('dedup-workflow-details')) byId('dedup-workflow-details').open = true;
      }
      byId('ai-page-title').textContent = definitions.find(([key]) => key === section)[1];
      byId('ai-settings-footer').hidden = section !== 'image-skills' || !byId('ai-settings-footer').childElementCount;
      byId('ai-return-smart').hidden = !smartOrigin;
      if (isAi) content.scrollTop = pageScroll.get(pageKey()) || 0;
      else if (workspace === 'schedule') byId('schedule-content').scrollTop = pageScroll.get('schedule') || 0;
      else if (previousWorkspace !== 'home') for (const [element, top] of homeScroll) if (element) element.scrollTop = top;
      app.emit('workspace:change', { workspace, section, toolPage });
    }
    const navigation = {
      get workspace() { return workspace; },
      get section() { return section; },
      get toolPage() { return toolPage; },
      isAiSection: value => aliases.has(value),
      isVisible: value => workspace === 'ai' && (['tools', 'artists', 'dedup', 'tool-catalog'].includes(value) ? section === 'tools' : section === value),
      openAi(value = section) {
        const next = value === 'skills' ? 'image-skills' : (value === 'artists' || value === 'dedup' || value === 'tool-catalog') ? 'tools' : value;
        const nextTool = value === 'artists' ? 'artists' : value === 'dedup' ? 'dedup' : value === 'tool-catalog' ? 'catalog' : toolPage;
        navigate('ai', definitions.some(([key]) => key === next) ? next : 'model', nextTool);
      },
      home() { smartOrigin = null; navigate('home'); },
      openSchedule() { smartOrigin = null; navigate('schedule'); },
      openModelFromSmart() {
        smartOrigin = { workspace, section, toolPage };
        window.closeAiModal(); smartOrigin.focus = document.activeElement;
        navigate('ai', 'model'); byId('ai-page-title').focus();
      },
    };
    app.runtime.navigation = navigation;
    window.openAiSettings = value => navigation.openAi(value);
    byId('workspace-home').addEventListener('click', () => navigation.home());
    byId('workspace-ai').addEventListener('click', () => { smartOrigin = null; navigation.openAi(); });
    byId('workspace-schedule').addEventListener('click', () => { navigation.openSchedule(); byId('schedule-page-title').focus(); });
    byId('settings-tab-tool-catalog').addEventListener('click', () => navigation.openAi('tool-catalog'));
    byId('ai-open-navigation').addEventListener('click', () => app.runtime.setDrawer(true));
    byId('ai-return-smart').addEventListener('click', () => {
      const origin = smartOrigin; smartOrigin = null;
      navigate(origin.workspace, origin.section, origin.toolPage);
      origin.focus?.focus();
      window.openAiModal(); byId('smart-model-settings').focus();
    });
    function tabKeys(list, vertical) {
      list.addEventListener('keydown', event => {
        const buttons = [...list.querySelectorAll('[role="tab"]')], index = buttons.indexOf(event.target);
        const previous = vertical ? 'ArrowUp' : 'ArrowLeft', next = vertical ? 'ArrowDown' : 'ArrowRight';
        if (index < 0 || ![previous, next, 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation();
        const target = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === previous ? -1 : 1) + buttons.length) % buttons.length;
        buttons[target].click(); buttons[target].focus();
      });
    }
    tabKeys(byId('ai-category-list'), true); tabKeys(toolsGroup.querySelector('.ai-tool-tabs'), false);
    byId('ai-category-list').addEventListener('click', () => {
      if (matchMedia('(max-width: 800px)').matches) byId('ai-page-title').focus();
    });
    navigate('home');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
