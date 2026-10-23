const BROWSE_PREFERENCES_KEY = 'twitter-tag-gallery.browse.v1';
    function readBrowsePreferences() {
      let saved = {};
      try { saved = JSON.parse(localStorage.getItem(BROWSE_PREFERENCES_KEY)) || {}; } catch (_) {}
      return {
        pageSize: Number.isInteger(saved.pageSize) && saved.pageSize >= 10 && saved.pageSize <= 200 ? saved.pageSize : 60,
        hoverPreview: saved.hoverPreview !== false,
        keyboardPaging: saved.keyboardPaging !== false
      };
    }
    const browsePreferences = readBrowsePreferences();
    const TAG_FILTERS_KEY = 'twitter-tag-gallery.tag-filters.v1';
    function readTagFilters() {
      let saved;
      try { saved = JSON.parse(localStorage.getItem(TAG_FILTERS_KEY)); } catch (_) {}
      const includeTags = Array.isArray(saved?.includeTags)
        ? [...new Set(saved.includeTags.filter(name => typeof name === 'string' && name.trim()))]
        : ['最近'];
      const categories = ['all', 'recent', 'artist', 'character', 'copyright', 'general', 'rating', 'source'];
      const labels = saved?.labels && typeof saved.labels === 'object' && !Array.isArray(saved.labels)
        ? Object.entries(saved.labels).filter(([name, label]) => includeTags.includes(name) && typeof label === 'string')
        : [];
      return { includeTags, tagCategory: categories.includes(saved?.tagCategory) ? saved.tagCategory : 'recent', labels };
    }
    const savedTagFilters = readTagFilters();
    const tagLabels = new Map(savedTagFilters.labels);
    const tagCanonicalNames = new Map();

    function saveTagFilters() {
      try {
        localStorage.setItem(TAG_FILTERS_KEY, JSON.stringify({
          includeTags: state.includeTags,
          tagCategory: state.tagCategory,
          labels: Object.fromEntries(state.includeTags.filter(name => tagLabels.has(name)).map(name => [name, tagLabels.get(name)]))
        }));
      } catch (_) { /* Filtering remains available when browser storage is blocked. */ }
    }

    function saveBrowsePreferences() {
      const status = document.getElementById('preferences-status');
      try {
        localStorage.setItem(BROWSE_PREFERENCES_KEY, JSON.stringify(browsePreferences));
        status.textContent = '已儲存瀏覽偏好';
      } catch (_) {
        status.textContent = '已套用。瀏覽器無法儲存偏好，下次開啟會使用預設值。';
      }
    }

    function syncBrowsePreferences() {
      document.getElementById('page-size-input').value = browsePreferences.pageSize;
      document.getElementById('preference-page-size').value = browsePreferences.pageSize;
      document.getElementById('preference-hover').checked = browsePreferences.hoverPreview;
      document.getElementById('preference-keyboard').checked = browsePreferences.keyboardPaging;
      document.getElementById('paging-hint').hidden = !browsePreferences.keyboardPaging;
    }

    function updateBrowsePreference(key, value) {
      browsePreferences[key] = value;
      saveBrowsePreferences();
      syncBrowsePreferences();
      hideHoverPreview();
    }

    function applyPageSize(inputId) {
      const input = document.getElementById(inputId);
      if (!input.reportValidity()) return;
      const size = Number(input.value);
      if (!Number.isInteger(size) || size < 10 || size > 200) return;
      browsePreferences.pageSize = size;
      state.pageSize = size;
      state.page = 1;
      saveBrowsePreferences();
      syncBrowsePreferences();
      document.getElementById('gallery-scroll').scrollTop = 0;
      loadImages();
    }

    let state = {
      includeTags: savedTagFilters.includeTags,
      folder: '__ALL__',
      aiStatus: 'all',
      tagCategory: savedTagFilters.tagCategory,
      page: 1,
      pageSize: browsePreferences.pageSize,
      items: [],
      total: 0,
      selectedIds: new Set(),
      detailIndex: -1,
      detailImageId: null,
      folders: [],
      stats: {}
    };

    let tagTimer = null;
    let imgTimer = null;
    let aiPollTimer = null;

    window.addEventListener('DOMContentLoaded', async () => {
      syncBrowsePreferences();
      document.querySelectorAll('.cat-pill').forEach(p => p.classList.toggle('active', p.dataset.cat === state.tagCategory));
      renderActiveFilters();
      setupHoverPreview();
      setupSelectionGestures();
      setupTagPagination();
      initializeDeleteNotice();
      setupDetailActions();
      window.addEventListener('keydown', handleBrowseKeydown);
      document.querySelector('.settings-tabs').addEventListener('keydown', handleSettingsTabKeydown);
      await Promise.all([loadTags(), loadImages(), loadStats(), loadFolders()]);
      startAiPolling();

    });

    function handleBrowseKeydown(e) {
      if (e.defaultPrevented || e.isComposing) return;
      if (document.getElementById('detail-more-actions').open) {
        if (e.key === 'Escape') {
          e.preventDefault();
          closeDetailActions(true);
          return;
        }
        if (['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      }
      if (e.key === 'Escape') {
        hideHoverPreview();
        if (document.getElementById('folder-browser-modal').classList.contains('open')) {
          closeFolderBrowserModal();
          return;
        }
        if (document.getElementById('time-picker-popover').classList.contains('open')) {
          closeTimePicker();
          return;
        }
        if (document.getElementById('schedule-modal').classList.contains('open')) {
          closeScheduleModal();
          return;
        }
        if (document.getElementById('detail-modal').classList.contains('open')) closeDetailModal();
        return;
      }
      const settings = document.getElementById('schedule-modal');
      if (settings.classList.contains('open')) {
        if (e.key === 'Tab' && !document.getElementById('folder-browser-modal').classList.contains('open') &&
            !document.getElementById('time-picker-popover').classList.contains('open')) {
          const focusable = [...settings.querySelectorAll('button, input, textarea, select, summary, [tabindex="0"]')]
            .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
          const first = focusable[0], last = focusable[focusable.length - 1];
          if (e.shiftKey && (document.activeElement === first || document.activeElement === settings.querySelector('[role="dialog"]'))) {
            e.preventDefault(); last?.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault(); first?.focus();
          }
        }
        return;
      }
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.repeat) return;
      if (e.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), video')) return;
      const detailOpen = document.getElementById('detail-modal').classList.contains('open');
      const otherModal = document.querySelector('.modal-backdrop.open:not(#detail-modal)');
      if (otherModal) return;
      if (e.key === 'Delete') {
        if (imageFileOperationBusy || selectionPointer?.active) return;
        const focusedId = Number(e.target.closest('.card')?.id?.replace('card-', ''));
        const ids = detailOpen ? [state.detailImageId].filter(Boolean)
          : state.selectedIds.size ? [...state.selectedIds]
          : state.items.some(item => item.id === focusedId) ? [focusedId] : [];
        if (ids.length) { e.preventDefault(); deleteImageFiles(ids); }
        return;
      }
      if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      const direction = e.key === 'ArrowLeft' ? -1 : 1;
      if (detailOpen) { e.preventDefault(); navigateDetail(direction); }
      else if (browsePreferences.keyboardPaging) { e.preventDefault(); changePage(direction); }
    }

    async function loadStats() {
      const base = async function () {
      const r = await fetch('/api/stats');
      const d = await r.json();
      state.stats = d;
      document.getElementById('st-total').textContent = d.total_images.toLocaleString();
      document.getElementById('st-root').textContent = d.root_unorganized.toLocaleString();
      document.getElementById('st-folder').textContent = d.folder_organized.toLocaleString();
      document.getElementById('st-ai').textContent = d.ai_tagged.toLocaleString();
      document.getElementById('st-suggest').textContent = d.suggested_folder_count.toLocaleString();
      updateAiBanner(d.ai_status);
      };
      return window.GalleryApp ? window.GalleryApp.run("loadStats", Array.from(arguments), base) : base();
    }

    async function loadFolders() {
      const base = async function () {
      const r = await fetch('/api/folders');
      state.folders = await r.json();
      document.getElementById('tab-btn-folders').textContent = `📁 實體資料夾 (${state.folders.length})`;

      const adl = document.getElementById('artist-datalist');
      adl.innerHTML = state.folders.map(f => `<option value="${escapeHtml(f.name)}">`).join('');
      renderFolderList();
      };
      return window.GalleryApp ? window.GalleryApp.run("loadFolders", Array.from(arguments), base) : base();
    }

    function renderFolderList() {
      const base = function () {
      const q = document.getElementById('folder-search-input').value.trim().toLowerCase();
      const box = document.getElementById('sidebar-folder-list');
      const special = [
        { key: '__ALL__', label: '🌐 全部圖片', count: state.stats.total_images || 0 },
        { key: '__ROOT__', label: '🚨 根目錄未整理檔案', count: state.stats.root_unorganized || 0 },
        { key: '__SUGGESTED__', label: '✨ AI 認出繪師可歸檔', count: state.stats.suggested_folder_count || 0 },
        { key: '__FOLDERS__', label: '✅ 所有已歸檔子資料夾', count: state.stats.folder_organized || 0 }
      ];

      let html = '';
      for (const sp of special) {
        const active = state.folder === sp.key ? 'active' : '';
        html += `
          <div class="folder-item ${active}" onclick="setFolderFilter('${sp.key}')">
            <span>${sp.label}</span>
            <span class="tag-count">${sp.count}</span>
          </div>
        `;
      }
      html += `<div style="margin:6px 0; border-top:1px solid var(--border);"></div>`;

      for (const f of state.folders) {
        if (q && !f.name.toLowerCase().includes(q)) continue;
        const active = state.folder === f.name ? 'active' : '';
        html += `
          <div class="folder-item ${active}" onclick="setFolderFilter('${escapeJs(f.name)}')">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">📁 ${escapeHtml(f.name)}</span>
            <span class="tag-count">${f.count}</span>
          </div>
        `;
      }
      box.innerHTML = html;
      };
      return window.GalleryApp ? window.GalleryApp.run("renderFolderList", Array.from(arguments), base) : base();
    }

    function switchSidebarTab(tab) {
      const base = function (tab) {
      document.getElementById('tab-btn-tags').classList.toggle('active', tab === 'tags');
      document.getElementById('tab-btn-folders').classList.toggle('active', tab === 'folders');
      document.getElementById('panel-tags').style.display = tab === 'tags' ? 'flex' : 'none';
      document.getElementById('panel-folders').style.display = tab === 'folders' ? 'flex' : 'none';
      };
      return window.GalleryApp ? window.GalleryApp.run("switchSidebarTab", Array.from(arguments), base) : base(tab);
    }

    function setTagCategory(cat, el) {
      const base = function (cat, el) {
      state.tagCategory = cat;
      document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
      if (el) el.classList.add('active');
      loadTags();
      if (cat === 'recent') {
        document.getElementById('sel-sort').value = 'mtime_desc';
        state.folder = '__ALL__';
        if (!state.includeTags.includes('最近')) {
          state.includeTags.unshift('最近');
        }
        state.page = 1;
        renderFolderList();
        renderActiveFilters();
        loadImages();
      }
      saveTagFilters();
      };
      return window.GalleryApp ? window.GalleryApp.run("setTagCategory", Array.from(arguments), base) : base(cat, el);
    }

    function debounceLoadTags() {
      clearTimeout(tagTimer);
      tagTimer = setTimeout(loadTags, 200);
    }

    let sidebarTags = [];
    const TAG_BATCH_SIZE = 40;
    let tagAbortController = null;
    let tagPaging = { loading: false, hasMore: false, error: false };
    let tagListObserver = null;

    function setupTagPagination() {
      const box = document.getElementById('sidebar-tag-list');
      box.addEventListener('scroll', () => {
        if (!tagPaging.error && box.clientHeight > 0 && box.scrollHeight - box.scrollTop - box.clientHeight < 160) loadMoreTags();
      }, { passive: true });
    }

    async function loadTags() {
      tagAbortController?.abort();
      tagAbortController = new AbortController();
      tagPaging = { category: state.tagCategory,
        q: document.getElementById('tag-search-input').value.trim(),
        offset: 0, loading: false, hasMore: true, error: false, signal: tagAbortController.signal };
      sidebarTags = [];
      document.getElementById('sidebar-tag-list').scrollTop = 0;
      return loadMoreTags();
    }

    async function loadMoreTags() {
      const paging = tagPaging;
      if (paging.loading || !paging.hasMore) return;
      paging.loading = true;
      paging.error = false;
      if (sidebarTags.length) updateTagLoadStatus(); else renderTagList();
      const params = new URLSearchParams({ category: paging.category, q: paging.q,
        limit: TAG_BATCH_SIZE, offset: paging.offset, paged: true });
      try {
        const r = await fetch(`/api/tags?${params}`, { signal: paging.signal, priority: 'high' });
        if (!r.ok) throw new Error('標籤載入失敗');
        const data = await r.json();
        if (paging !== tagPaging) return;
        if (!Array.isArray(data.items)) throw new Error('標籤回應格式錯誤');
        const existing = new Set(sidebarTags.map(tag => tag.name));
        const added = data.items.filter(tag => {
          if (existing.has(tag.name)) return false;
          existing.add(tag.name);
          return true;
        });
        paging.hasMore = Boolean(data.has_more) && data.next_offset > paging.offset && data.items.length > 0;
        paging.offset = data.next_offset;
        if (sidebarTags.length) {
          sidebarTags.push(...added);
          rememberSidebarTags(added);
          document.getElementById('tag-list-status').insertAdjacentHTML('beforebegin', added.map(renderSidebarTag).join(''));
        } else {
          renderTagList(added);
        }
        return true;
      } catch (error) {
        if (paging !== tagPaging || error.name === 'AbortError') return;
        paging.error = true;
        return false;
      } finally {
        if (paging === tagPaging) {
          paging.loading = false;
          updateTagLoadStatus();
        }
      }
    }

    function rememberSidebarTags(tags) {
      tags.forEach(t => {
        const label = tagDisplayLabel(t);
        for (const name of t.aliases || [t.name]) {
          tagLabels.set(name, label);
          tagCanonicalNames.set(name, t.name);
        }
      });
      state.includeTags = [...new Set(state.includeTags.map(name => tagCanonicalNames.get(name) || name))];
      if (tags.length) saveTagFilters();
      renderActiveFilters();
    }

    function updateTagLoadStatus() {
      tagListObserver?.disconnect();
      const status = document.getElementById('tag-list-status');
      if (!status) return;
      const { loading, hasMore, error } = tagPaging;
      document.getElementById('sidebar-tag-list').setAttribute('aria-busy', String(loading));
      status.hidden = !loading && !hasMore && !error && sidebarTags.length > 0;
      status.innerHTML = loading ? '讀取標籤中…' : error
        ? '<button type="button" class="tag-load-more" onclick="loadMoreTags()">載入失敗，點此重試</button>'
        : hasMore ? '<button type="button" class="tag-load-more" onclick="loadMoreTags()">載入更多標籤</button>'
        : '找不到符合的標籤';
      if (hasMore && !loading && !error && typeof IntersectionObserver === 'function') {
        tagListObserver ||= new IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting && entry.target === document.getElementById('tag-list-status'))) loadMoreTags();
        }, { root: document.getElementById('sidebar-tag-list'), rootMargin: '0px 0px 160px 0px' });
        tagListObserver.observe(status);
      }
    }

    function renderTagList(tags = sidebarTags) {
      sidebarTags = tags;
      rememberSidebarTags(tags);
      document.getElementById('sidebar-tag-list').innerHTML = tags.map(renderSidebarTag).join('')
        + '<div id="tag-list-status" class="tag-list-status" role="status" aria-live="polite"></div>';
      updateTagLoadStatus();
    }

    function renderSidebarTag(t) {
        const mainLabel = tagDisplayLabel(t);
        const subLabel = (t.zh_name && t.category !== 'recent' && t.category !== 'artist' && t.zh_name !== t.name) ? t.name : '';
        const included = state.includeTags.includes(t.name);
        return `
          <div class="tag-item ${included ? 'included' : ''}">
            <button class="tag-toggle" aria-pressed="${included}" onclick="toggleIncludeTag('${escapeJs(t.name)}')" title="${included ? '取消篩選' : '加入篩選'}：${escapeHtml(mainLabel)}">
              <span class="tag-item-left">
              <span class="cat-dot cat-${t.category}"></span>
              <span class="tag-label-zh">${escapeHtml(mainLabel)}</span>
              ${subLabel ? `<span class="tag-label-en">${escapeHtml(subLabel)}</span>` : ''}
              </span>
              ${included ? '<span class="tag-state" aria-label="已選取">✓</span>' : ''}
              <span class="tag-count">${t.count}</span>
            </button>
          </div>
        `;
    }

    function tagDisplayLabel(tag) {
      const label = tag.zh_name || tag.name;
      return tag.category === 'artist' ? label.replace(/^現有繪師[：:]\s*/, '') : label;
    }

    function toggleIncludeTag(tname) {
      tname = tagCanonicalNames.get(tname) || tname;
      const selected = state.includeTags.includes(tname);
      state.includeTags = state.includeTags.filter(x => x !== tname);
      if (!selected) {
        state.includeTags.push(tname);
        if (tname === '最近' || tname.startsWith('最近:')) {
          document.getElementById('sel-sort').value = 'mtime_desc';
          state.folder = '__ALL__';
          renderFolderList();
        }
      }
      state.page = 1;
      saveTagFilters();
      renderActiveFilters();
      renderTagList();
      loadImages();
    }

    function setFolderFilter(f) {
      const base = function (f) {
      state.folder = f;
      state.page = 1;
      renderFolderList();
      renderActiveFilters();
      loadImages();
      };
      return window.GalleryApp ? window.GalleryApp.run("setFolderFilter", Array.from(arguments), base) : base(f);
    }

    function setAiFilter(val) {
      state.aiStatus = val;
      resetPageAndLoad();
    }

    function clearAllFilters() {
      state.includeTags = [];
      state.folder = '__ALL__';
      document.getElementById('main-search').value = '';
      state.aiStatus = 'all';
      state.page = 1;
      saveTagFilters();
      renderFolderList();
      renderActiveFilters();
      renderTagList();
      loadImages();
    }

    function clearTagFilters() {
      state.includeTags = [];
      state.page = 1;
      saveTagFilters();
      renderActiveFilters();
      renderTagList();
      loadImages();
    }

    function renderActiveFilters() {
      const base = function () {
      const bar = document.getElementById('active-filters-bar');
      let chips = [];

      if (state.folder !== '__ALL__') {
        const fmap = {
          '__ROOT__': '🚨 根目錄未整理檔案',
          '__FOLDERS__': '📁 所有已歸檔資料夾',
          '__SUGGESTED__': '✨ AI 認出繪師待歸檔'
        };
        const flabel = fmap[state.folder] || `📁 資料夾: ${state.folder}`;
        chips.push(`<button class="filter-chip inc" onclick="setFolderFilter('__ALL__')" aria-label="移除資料夾條件：${escapeHtml(flabel)}">${escapeHtml(flabel)} <span class="remove-mark" aria-hidden="true">×</span></button>`);
      }

      for (const t of state.includeTags) {
        const disp = t === '最近' ? '最近' : tagLabels.get(t) || t;
        chips.push(`<button class="filter-chip inc" onclick="toggleIncludeTag('${escapeJs(t)}')" aria-label="移除標籤：${escapeHtml(disp)}" title="${escapeHtml(t)}">${escapeHtml(disp)} <span class="remove-mark" aria-hidden="true">×</span></button>`);
      }
      bar.innerHTML = chips.join('') || '<span class="muted">尚未套用標籤或資料夾條件</span>';
      document.getElementById('active-filter-count').textContent = `標籤與資料夾 · ${chips.length}`;
      document.getElementById('btn-clear-tags').hidden = !state.includeTags.length;
      };
      return window.GalleryApp ? window.GalleryApp.run("renderActiveFilters", Array.from(arguments), base) : base();
    }

    function debounceLoadImages() {
      clearTimeout(imgTimer);
      imgTimer = setTimeout(() => {
        state.page = 1;
        loadImages();
      }, 250);
    }

    function resetPageAndLoad() {
      state.page = 1;
      loadImages();
    }

    let imageLoadSequence = 0;
    let imageAbortController = null;
    const deletedImageIds = new Set();

    function invalidateImageRequests() {
      imageLoadSequence++;
      imageAbortController?.abort();
      imageAbortController = null;
    }

    async function loadImages(options = {}) {
      const base = async function (options = {}) {
      hideHoverPreview();
      const sequence = ++imageLoadSequence;
      imageAbortController?.abort();
      imageAbortController = new AbortController();
      document.getElementById('image-grid').setAttribute('aria-busy', 'true');
      const q = document.getElementById('main-search').value.trim();
      const sort = document.getElementById('sel-sort').value;

      const params = new URLSearchParams({
        include_tags: state.includeTags.join(','),
        folder: state.folder,
        ai_status: state.aiStatus,
        q: q,
        sort: sort,
        page: state.page,
        page_size: state.pageSize,
        compact: true
      });

      try {
        const r = await fetch(`/api/images?${params.toString()}`, { signal: imageAbortController.signal, cache: 'no-store' });
        if (!r.ok) throw new Error('圖片載入失敗');
        const data = await r.json();
        if (sequence !== imageLoadSequence) return;
        const removedCount = data.items.filter(item => deletedImageIds.has(item.id)).length;
        state.items = data.items.filter(item => !deletedImageIds.has(item.id));
        state.total = removedCount ? Math.min(state.total, Math.max(0, data.total - removedCount)) : data.total;
        if (document.getElementById('detail-modal').classList.contains('open')) {
          state.detailIndex = state.items.findIndex(item => item.id === state.detailImageId);
        }
        const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
        if (state.page > totalPages) { state.page = totalPages; return loadImages(options); }
        renderGrid();
        renderPagination();
        if (removedCount && !options.retriedDeletion) return loadImages({ ...options, retriedDeletion: true });
        return true;
      } catch (error) {
        if (sequence === imageLoadSequence && error.name !== 'AbortError') {
          if (!options.preserveOnError) document.getElementById('page-info').textContent = '圖片載入失敗，請重新套用或翻頁重試。';
          return false;
        }
      } finally {
        if (sequence === imageLoadSequence) document.getElementById('image-grid').setAttribute('aria-busy', 'false');
      }
      };
      return window.GalleryApp ? window.GalleryApp.run("loadImages", Array.from(arguments), base) : base();
    }

    function formatMtime(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mi = String(d.getMinutes()).padStart(2, '0');
      return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
    }

    function renderGrid() {
      const base = function () {
      finishSelectionPaint();
      const grid = document.getElementById('image-grid');
      if (!state.items.length) {
        grid.innerHTML = `<div style="grid-column:1/-1; text-align:center; padding:60px; color:var(--text-muted);">目前篩選條件下沒有符合的圖片</div>`;
        return;
      }

      grid.innerHTML = state.items.map((item, idx) => {
        const isSel = state.selectedIds.has(item.id);
        let badges = '';
        if (item.media_type !== 'image') {
          badges += `<span class="mini-badge media">${item.media_type.toUpperCase()}</span>`;
        }
        if (item.rating === 'explicit') {
          badges += `<span class="mini-badge r18">R-18</span>`;
        } else if (item.rating === 'questionable' || item.rating === 'sensitive') {
          badges += `<span class="mini-badge sensitive">R-15</span>`;
        }

        let pills = [];
        const mtimeStr = formatMtime(item.mtime);
        if (mtimeStr) {
          pills.push(`<span class="pill" style="background:rgba(56,189,248,0.14); color:#7dd3fc; border:1px solid rgba(56,189,248,0.3);" title="檔案最後修改/編輯時間">🕒 ${mtimeStr}</span>`);
        }
        for (const a of item.artists.slice(0, 1)) {
          const label = tagDisplayLabel(a);
          pills.push(`<span class="pill artist" onclick="event.stopPropagation(); toggleIncludeTag('${escapeJs(a.name)}')" title="點擊篩選此畫師">🎨 ${escapeHtml(label)}</span>`);
        }
        if (item.suggested_folder && !item.folder) {
          pills.push(`<span class="pill suggest" onclick="event.stopPropagation(); openDetailModal(${idx})" title="已辨識出目標繪師資料夾">✨ 建議歸檔: ${escapeHtml(item.suggested_folder)}</span>`);
        }
        return `
          <div class="card ${isSel ? 'selected' : ''}" id="card-${item.id}">
            <div class="thumb-box" data-index="${idx}" tabindex="0" role="button" aria-label="檢視圖片：${escapeHtml(item.filename)}" onclick="handleImageClick(event, ${idx})" onkeydown="if(event.target===this && (event.key==='Enter' || event.key===' ')) { event.preventDefault(); event.stopImmediatePropagation(); handleImageClick(event, ${idx}); }">
              <div class="card-check" role="checkbox" tabindex="0" aria-checked="${isSel}" aria-label="選取 ${escapeHtml(item.filename)}" title="Shift 點選可連選；按住左鍵拖過圖片可連續勾選或取消" onclick="event.stopPropagation(); toggleSelect(${item.id}, event)" onkeydown="if(event.key==='Enter' || event.key===' ') { event.preventDefault(); event.stopImmediatePropagation(); toggleSelect(${item.id}, event); }">
                ${isSel ? '✓' : ''}
              </div>
              <div class="badge-top-right">${badges}</div>
              <img src="/api/thumb/${item.id}" loading="lazy" decoding="async" fetchpriority="low" draggable="false" alt="${escapeHtml(item.filename)}" />
            </div>
            <div class="card-body">
              <div class="card-title" title="${escapeHtml(item.rel_path)}">
                ${item.folder ? `📁 [${escapeHtml(item.folder)}] ` : ''}${escapeHtml(item.filename)}
              </div>
              <div class="card-tags">${pills.join('')}</div>
            </div>
          </div>
        `;
      }).join('');
      };
      return window.GalleryApp ? window.GalleryApp.run("renderGrid", Array.from(arguments), base) : base();
    }

    function renderPagination() {
      const base = function () {
      const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
      document.getElementById('page-info').textContent = `共 ${state.total.toLocaleString()} 張符合條件 (本頁顯示 ${state.items.length} 張)`;
      document.getElementById('page-num-label').textContent = `第 ${state.page} / ${totalPages} 頁`;
      document.getElementById('btn-prev').disabled = state.page <= 1;
      document.getElementById('btn-next').disabled = state.page >= totalPages;
      };
      return window.GalleryApp ? window.GalleryApp.run("renderPagination", Array.from(arguments), base) : base();
    }

    function changePage(delta) {
      const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
      const next = state.page + delta;
      if (next >= 1 && next <= totalPages) {
        state.page = next;
        document.getElementById('gallery-scroll').scrollTop = 0;
        loadImages();
      }
    }

    /* One delayed preview, outside the scrolling grid so it never moves cards. */
    let hoverTimer = null;
    let hoverTarget = null;
    let hoverGeneration = 0;
    let hoverPoint = { x: 0, y: 0 };

    function hideHoverPreview() {
      clearTimeout(hoverTimer);
      hoverGeneration++;
      hoverTarget = null;
      document.getElementById('hover-preview').hidden = true;
      document.getElementById('hover-preview-image').removeAttribute('src');
    }

    function positionHoverPreview() {
      const preview = document.getElementById('hover-preview');
      const width = preview.offsetWidth, height = preview.offsetHeight;
      const gap = 20, margin = 12;
      let left = hoverPoint.x + gap;
      if (left + width > window.innerWidth - margin) left = hoverPoint.x - width - gap;
      preview.style.left = `${Math.max(margin, Math.min(left, window.innerWidth - width - margin))}px`;
      preview.style.top = `${Math.max(margin, Math.min(hoverPoint.y - height / 2, window.innerHeight - height - margin))}px`;
    }

    function scheduleHoverPreview(target, point) {
      if (selectionPointer?.active) return;
      if (hoverTarget === target) return;
      hideHoverPreview();
      if (!browsePreferences.hoverPreview || document.querySelector('.modal-backdrop.open')) return;
      hoverTarget = target;
      hoverPoint = point;
      const generation = hoverGeneration;
      hoverTimer = setTimeout(() => {
        const item = state.items[Number(target.dataset.index)];
        if (!item || !target.isConnected) return;
        const source = item.media_type === 'image' ? `/api/raw/${item.id}` : `/api/thumb/${item.id}`;
        const loader = new Image();
        loader.onload = () => {
          if (generation !== hoverGeneration || hoverTarget !== target || !target.isConnected) return;
          document.getElementById('hover-preview-image').src = source;
          document.getElementById('hover-preview-caption').textContent = item.filename;
          document.getElementById('hover-preview').hidden = false;
          positionHoverPreview();
        };
        loader.onerror = () => { if (generation === hoverGeneration) hideHoverPreview(); };
        loader.src = source;
      }, 380);
    }

    function setupHoverPreview() {
      const grid = document.getElementById('image-grid');
      grid.addEventListener('pointerover', e => {
        if (e.pointerType !== 'mouse') return;
        const target = e.target.closest('.thumb-box');
        if (target && !e.target.closest('.card-check')) scheduleHoverPreview(target, { x: e.clientX, y: e.clientY });
      });
      grid.addEventListener('pointermove', e => {
        if (hoverTarget) {
          hoverPoint = { x: e.clientX, y: e.clientY };
          if (!document.getElementById('hover-preview').hidden) positionHoverPreview();
        }
      });
      grid.addEventListener('pointerout', e => {
        if (hoverTarget && !hoverTarget.contains(e.relatedTarget)) hideHoverPreview();
      });
      grid.addEventListener('focusin', e => {
        const target = e.target.closest('.thumb-box');
        if (target) {
          const rect = target.getBoundingClientRect();
          scheduleHoverPreview(target, { x: rect.right, y: rect.top + rect.height / 2 });
        }
      });
      grid.addEventListener('focusout', hideHoverPreview);
      document.addEventListener('scroll', hideHoverPreview, true);
      window.addEventListener('resize', hideHoverPreview);
      window.addEventListener('blur', hideHoverPreview);
      new MutationObserver(() => {
        if (document.querySelector('.modal-backdrop.open')) hideHoverPreview();
      }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
    }

    /* Selection & Batch Operations */
    let imageFileOperationBusy = false;

    function initializeDeleteNotice() {
      document.getElementById('file-delete-notice').hidden = true;
      try {
        localStorage.removeItem('twitter-tag-gallery.last-deletion');
        localStorage.removeItem('twitter-tag-gallery.last-deletion.dismissed');
      } catch (_) {}
    }

    function dismissDeleteNotice() {
      if (imageFileOperationBusy) return;
      document.getElementById('file-delete-notice').hidden = true;
    }

    function showDeleteNotice(message) {
      document.getElementById('file-delete-notice').hidden = false;
      document.getElementById('file-delete-status').textContent = message;
    }

    function setImageFileBusy(busy) {
      imageFileOperationBusy = busy;
      for (const id of ['btn-delete-selected', 'btn-delete-single', 'btn-dismiss-delete-notice']) {
        const el = document.getElementById(id);
        if (el) el.disabled = busy;
      }
    }

    async function imageFileRequest(url, options) {
      if (window.GalleryApp) return window.GalleryApp.api(url, options);
      const response = await fetch(url, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || '??????');
      return data;
    }

    function deleteSelectedImages() { return deleteImageFiles([...state.selectedIds]); }
    function deleteCurrentImage() {
      if (document.getElementById('detail-modal').classList.contains('open') && state.detailImageId) {
        return deleteImageFiles([state.detailImageId]);
      }
    }

    async function deleteImageFiles(imageIds) {
      if (imageFileOperationBusy || !imageIds.length) return;
      const ids = [...new Set(imageIds)];
      const visible = state.items.filter(item => ids.includes(item.id));
      hideHoverPreview();
      document.getElementById('file-delete-notice').hidden = true;
      setImageFileBusy(true);
      let unlocked = false;
      try {
        const result = await imageFileRequest('/api/images/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: ids })
        });
        const removed = new Set(result.deleted_ids);
        const viewingRemoved = document.getElementById('detail-modal').classList.contains('open') && removed.has(state.detailImageId);
        const viewedIndex = state.items.findIndex(item => item.id === state.detailImageId);
        const nextItem = viewingRemoved ? state.items.slice(viewedIndex + 1).find(item => !removed.has(item.id)) : null;
        const previousItem = viewingRemoved ? state.items.slice(0, Math.max(0, viewedIndex)).reverse().find(item => !removed.has(item.id)) : null;
        const hasFollowingPage = state.page * state.pageSize < state.total;
        invalidateImageRequests();
        for (const id of removed) deletedImageIds.add(id);
        for (const id of removed) state.selectedIds.delete(id);
        state.items = state.items.filter(item => !removed.has(item.id));
        state.total = Math.max(0, state.total - visible.filter(item => removed.has(item.id)).length);
        state.page = Math.min(state.page, Math.max(1, Math.ceil(state.total / state.pageSize)));
        for (const id of removed) document.getElementById(`card-${id}`)?.remove?.();
        if (!state.items.length) {
          updateSelectionUI(true);
        } else {
          const cards = document.getElementById('image-grid').querySelectorAll?.('.card') || [];
          if (cards.length === state.items.length) {
            state.items.forEach((item, idx) => {
              const thumb = document.getElementById(`card-${item.id}`)?.querySelector?.('.thumb-box');
              if (thumb) {
                thumb.dataset.index = String(idx);
                thumb.setAttribute('onclick', `handleImageClick(event, ${idx})`);
              }
            });
            updateSelectionUI(false);
          } else {
            updateSelectionUI(true);
          }
        }
        renderPagination();
        let deferredDetail = null;
        let detailRequest = Promise.resolve();
        if (viewingRemoved) {
          const target = nextItem || (!hasFollowingPage && previousItem);
          if (target) {
            state.detailImageId = target.id;
            state.detailIndex = state.items.findIndex(item => item.id === target.id);
            unlocked = true;
            setImageFileBusy(false);
            detailRequest = refreshDetailView(target.id);
          } else if (state.total || state.items.length) {
            deferredDetail = { sequence: ++detailLoadSequence, index: hasFollowingPage ? Math.max(0, viewedIndex) : -1 };
            closeDetailActions();
            state.detailImageId = null;
            state.detailIndex = -1;
            currentDetailData = null;
            document.getElementById('dt-filename').textContent = '??????????';
          } else {
            closeDetailModal();
            unlocked = true;
            setImageFileBusy(false);
          }
        } else {
          unlocked = true;
          setImageFileBusy(false);
        }
        const refreshed = await Promise.allSettled([loadImages({ preserveOnError: true }), loadStats(), loadFolders(), loadTags(), detailRequest]);
        if (deferredDetail && deferredDetail.sequence === detailLoadSequence && document.getElementById('detail-modal').classList.contains('open')) {
          const target = deferredDetail.index < 0 ? state.items.at(-1) : state.items[Math.min(deferredDetail.index, state.items.length - 1)];
          if (target) {
            state.detailImageId = target.id;
            state.detailIndex = state.items.findIndex(item => item.id === target.id);
            unlocked = true;
            setImageFileBusy(false);
            await refreshDetailView(target.id);
          } else {
            closeDetailModal();
          }
        }
        if (refreshed[0].status === 'rejected' || refreshed[0].value === false) {
          showDeleteNotice('???????????????????????????');
        }
      } catch (error) {
        showDeleteNotice(`?????${error.message}`);
      } finally {
        if (!unlocked) setImageFileBusy(false);
      }
    }

    let selectionAnchorId = null;
    let selectionPointer = null;
    let suppressSelectionClick = false;

    function handleImageClick(event, index) {
      if (event.shiftKey) {
        event.preventDefault();
        toggleSelect(state.items[index].id, event);
      } else openDetailModal(index);
    }

    function toggleSelect(id, event = {}) {
      const anchor = state.items.findIndex(item => item.id === selectionAnchorId);
      const target = state.items.findIndex(item => item.id === id);
      if (event.shiftKey && anchor >= 0 && target >= 0) {
        for (const item of state.items.slice(Math.min(anchor, target), Math.max(anchor, target) + 1)) state.selectedIds.add(item.id);
      } else {
        if (state.selectedIds.has(id)) state.selectedIds.delete(id);
        else state.selectedIds.add(id);
        selectionAnchorId = id;
      }
      hideHoverPreview();
      updateSelectionUI(false);
    }

    function selectionSegmentHitsRect(from, to, rect) {
      // Clip the pointer segment so fast sweeps also include skipped cards.
      let low = 0, high = 1;
      const dx = to.x - from.x, dy = to.y - from.y;
      for (const [p, q] of [[-dx, from.x - rect.left], [dx, rect.right - from.x], [-dy, from.y - rect.top], [dy, rect.bottom - from.y]]) {
        if (!p) { if (q < 0) return false; continue; }
        const value = q / p;
        if (p < 0) low = Math.max(low, value);
        else high = Math.min(high, value);
        if (low > high) return false;
      }
      return true;
    }

    function paintSelectionTo(point) {
      const pointer = selectionPointer;
      if (!pointer?.active) return;
      const viewport = document.getElementById('gallery-scroll').getBoundingClientRect();
      const inside = new Set();
      for (const card of document.getElementById('image-grid').querySelectorAll('.card')) {
        const id = Number(card.id.slice(5));
        const rect = card.getBoundingClientRect();
        const visible = { left: Math.max(viewport.left, rect.left), right: Math.min(viewport.right, rect.right),
          top: Math.max(viewport.top, rect.top), bottom: Math.min(viewport.bottom, rect.bottom) };
        if (visible.left >= visible.right || visible.top >= visible.bottom) continue;
        if (point.x >= visible.left && point.x <= visible.right && point.y >= visible.top && point.y <= visible.bottom) inside.add(id);
        // Do not repeatedly toggle while moving within a card. Once the
        // pointer leaves and sweeps back in, that card can change again.
        if (pointer.inside.has(id) || !selectionSegmentHitsRect(pointer.last, point, visible)) continue;
        const selected = pointer.visited.has(id) ? !state.selectedIds.has(id) : pointer.select;
        pointer.visited.add(id);
        if (selected) state.selectedIds.add(id);
        else state.selectedIds.delete(id);
      }
      pointer.inside = inside;
      pointer.last = point;
      updateSelectionUI(false);
    }

    function finishSelectionPaint() {
      const pointer = selectionPointer;
      if (!pointer) return;
      selectionPointer = null;
      const grid = document.getElementById('image-grid');
      grid.classList.remove('selection-painting');
      if (grid.hasPointerCapture?.(pointer.id)) grid.releasePointerCapture(pointer.id);
      if (pointer.active) {
        suppressSelectionClick = true;
        setTimeout(() => { suppressSelectionClick = false; }, 0);
        updateSelectionUI(false);
      }
    }

    function setupSelectionGestures() {
      const grid = document.getElementById('image-grid');
      grid.addEventListener('pointerdown', event => {
        if (event.pointerType !== 'mouse' || event.button !== 0 || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
        const thumbnail = event.target.closest('.thumb-box');
        const item = thumbnail && state.items[Number(thumbnail.dataset.index)];
        if (!item || event.target.closest('button, a, input, .pill')) return;
        selectionPointer = { id: event.pointerId, itemId: item.id, select: !state.selectedIds.has(item.id),
          active: false, last: { x: event.clientX, y: event.clientY }, visited: new Set(), inside: new Set([item.id]) };
      });
      document.addEventListener('pointermove', event => {
        const pointer = selectionPointer;
        if (!pointer || event.pointerId !== pointer.id) return;
        if (!(event.buttons & 1)) { finishSelectionPaint(); return; }
        const point = { x: event.clientX, y: event.clientY };
        if (!pointer.active) {
          if (Math.hypot(point.x - pointer.last.x, point.y - pointer.last.y) < 6) return;
          pointer.active = true;
          selectionAnchorId = pointer.itemId;
          if (pointer.select) state.selectedIds.add(pointer.itemId);
          else state.selectedIds.delete(pointer.itemId);
          pointer.visited.add(pointer.itemId);
          hideHoverPreview();
          grid.classList.add('selection-painting');
          grid.setPointerCapture?.(pointer.id);
        }
        event.preventDefault();
        paintSelectionTo(point);
      }, { passive: false });
      document.addEventListener('pointerup', event => { if (event.pointerId === selectionPointer?.id) finishSelectionPaint(); });
      document.addEventListener('pointercancel', finishSelectionPaint);
      grid.addEventListener('lostpointercapture', finishSelectionPaint);
      grid.addEventListener('dragstart', event => { if (event.target.closest('.thumb-box')) event.preventDefault(); });
      document.addEventListener('click', event => {
        if (!suppressSelectionClick) return;
        suppressSelectionClick = false;
        event.preventDefault();
        event.stopImmediatePropagation();
      }, true);
      document.getElementById('gallery-scroll').addEventListener('scroll', () => {
        if (selectionPointer?.active) paintSelectionTo(selectionPointer.last);
      });
      window.addEventListener('blur', finishSelectionPaint);
      window.addEventListener('resize', finishSelectionPaint);
    }

    function selectAllCurrentPage() {
      for (const item of state.items) state.selectedIds.add(item.id);
      updateSelectionUI(false);
    }

    function clearSelection() {
      state.selectedIds.clear();
      selectionAnchorId = null;
      updateSelectionUI(false);
    }

    function updateSelectionUI(render = true) {
      const bar = document.getElementById('batch-bar');
      // Keep the grid stationary under the held pointer; reveal/hide the bar
      // after release instead of moving the first row halfway through a sweep.
      if (!selectionPointer?.active) bar.classList.toggle('active', state.selectedIds.size > 0);
      document.getElementById('batch-count').textContent = state.selectedIds.size;
      if (render) renderGrid();
      else for (const item of state.items) {
        const card = document.getElementById(`card-${item.id}`);
        const selected = state.selectedIds.has(item.id);
        card?.classList.toggle('selected', selected);
        const check = card?.querySelector?.('.card-check');
        if (check) { check.textContent = selected ? '✓' : ''; check.setAttribute('aria-checked', String(selected)); }
      }
    }

    function openBatchTagModal(defaultCat) {
      document.getElementById('bm-category').value = defaultCat || 'artist';
      document.getElementById('bm-input').value = '';
      document.getElementById('bm-also-move').checked = (defaultCat === 'artist');
      document.getElementById('batch-modal-title').textContent =
        defaultCat === 'artist' ? `🎨 批次設定畫師標籤 (${state.selectedIds.size} 張)` : `🏷️ 批次新增標籤 (${state.selectedIds.size} 張)`;
      document.getElementById('batch-modal').classList.add('open');
      setTimeout(() => document.getElementById('bm-input').focus(), 50);
    }

    function openBatchMoveModal() {
      document.getElementById('bm-category').value = 'artist';
      document.getElementById('bm-input').value = '';
      document.getElementById('bm-also-move').checked = true;
      document.getElementById('batch-modal-title').textContent = `📂 批次搬移至繪師資料夾並標記畫師 (${state.selectedIds.size} 張)`;
      document.getElementById('batch-modal').classList.add('open');
      setTimeout(() => document.getElementById('bm-input').focus(), 50);
    }

    function closeBatchModal() {
      document.getElementById('batch-modal').classList.remove('open');
    }

    async function submitBatchTag() {
      const cat = document.getElementById('bm-category').value;
      const raw = document.getElementById('bm-input').value.trim();
      const alsoMove = document.getElementById('bm-also-move').checked;
      if (!raw) return alert('請輸入畫師或標籤名稱');

      const names = raw.split(',').map(s => s.trim()).filter(Boolean);
      const addTags = names.map(n => ({ name: n, category: cat }));

      const r = await fetch('/api/tags/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_ids: Array.from(state.selectedIds),
          add_tags: addTags,
          move_to_artist_folder: alsoMove ? names[0] : null
        })
      });
      const res = await r.json();
      closeBatchModal();
      clearSelection();
      await loadStats();
      await loadFolders();
      await loadTags();
      await loadImages();
    }

    /* Lightbox Detail View */
    let currentDetailData = null;
    let currentDecensorPreviewUrl = null;
    let showingDecensoredPreview = false;

    function closeDetailActions(restoreFocus = false) {
      const actions = document.getElementById('detail-more-actions');
      if (!actions.open) return;
      actions.open = false;
      if (restoreFocus) document.getElementById('btn-detail-more').focus();
    }

    function setupDetailActions() {
      const actions = document.getElementById('detail-more-actions');
      const trigger = document.getElementById('btn-detail-more');
      const buttons = () => [...actions.querySelectorAll('button')].filter(button => !button.disabled);
      trigger.addEventListener('keydown', event => {
        if (event.key !== 'ArrowDown') return;
        event.preventDefault();
        event.stopPropagation();
        actions.open = true;
        buttons()[0]?.focus();
      });
      actions.addEventListener('keydown', event => {
        if (!actions.open || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const items = buttons();
        const index = items.indexOf(document.activeElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      });
      document.addEventListener('click', event => {
        if (!actions.contains(event.target)) closeDetailActions();
      });
      actions.addEventListener('focusout', event => {
        if (!actions.contains(event.relatedTarget)) closeDetailActions();
      });
    }

    const detailMediaCacheBust = new Map();
    const detailPreloadCache = new Map();
    const DETAIL_PRELOAD_LIMIT = 12;

    function canDecodeDetailImages() {
      return typeof Image === 'function' && typeof Image.prototype?.decode === 'function';
    }

    function getDetailMediaUrl(imageId) {
      const bust = detailMediaCacheBust.get(imageId);
      return bust ? `/api/raw/${imageId}?t=${bust}` : `/api/raw/${imageId}`;
    }

    function invalidateDetailMediaCache(imageId) {
      const prevUrl = getDetailMediaUrl(imageId);
      detailPreloadCache.delete(prevUrl);
      detailMediaCacheBust.set(imageId, Date.now());
    }

    function rememberDetailPreload(src, entry) {
      if (detailPreloadCache.has(src)) detailPreloadCache.delete(src);
      detailPreloadCache.set(src, entry);
      while (detailPreloadCache.size > DETAIL_PRELOAD_LIMIT) {
        const oldestKey = detailPreloadCache.keys().next().value;
        detailPreloadCache.delete(oldestKey);
      }
    }

    function createDetailImagePreload(src) {
      const loader = new Image();
      loader.decoding = 'async';
      const entry = { loader, promise: null, used: false, ok: null };
      entry.promise = new Promise(resolve => {
        let settled = false;
        const finish = ok => {
          if (settled) return;
          settled = true;
          loader.onload = null;
          loader.onerror = null;
          entry.ok = ok;
          if (!ok && detailPreloadCache.get(src) === entry) {
            detailPreloadCache.delete(src);
          }
          resolve({ ok, img: loader, entry });
        };
        loader.onload = () => {
          if (typeof loader.decode === 'function') {
            loader.decode().then(() => finish(true), () => finish(true));
          } else {
            finish(true);
          }
        };
        loader.onerror = () => finish(false);
        loader.src = src;
        if (loader.complete && loader.naturalWidth > 0) {
          if (typeof loader.decode === 'function') {
            loader.decode().then(() => finish(true), () => finish(true));
          } else {
            finish(true);
          }
        }
      });
      rememberDetailPreload(src, entry);
      return entry;
    }

    function warmDetailImage(src) {
      if (!canDecodeDetailImages() || !src) return;
      const existing = detailPreloadCache.get(src);
      if (existing && !existing.used && existing.ok !== false) {
        rememberDetailPreload(src, existing);
        return;
      }
      createDetailImagePreload(src);
    }

    function preloadDetailImage(src) {
      if (!canDecodeDetailImages()) {
        return Promise.resolve({ ok: true, img: null, entry: null });
      }
      const existing = detailPreloadCache.get(src);
      if (existing && !existing.used && existing.ok !== false) {
        existing.used = true;
        rememberDetailPreload(src, existing);
        return existing.promise;
      }
      const entry = createDetailImagePreload(src);
      entry.used = true;
      return entry.promise;
    }

    function preloadAdjacentDetailImages(centerIndex) {
      if (!canDecodeDetailImages() || centerIndex < 0 || !state.items.length) return;
      for (const offset of [1, -1, 2, -2]) {
        const neighbor = state.items[centerIndex + offset];
        if (neighbor && neighbor.media_type !== 'video' && !neighbor.file_missing) {
          warmDetailImage(getDetailMediaUrl(neighbor.id));
        }
      }
    }

    async function openDetailModal(idx) {
      hideHoverPreview();
      const item = state.items[idx];
      if (!item) return;
      state.detailIndex = idx;

      document.getElementById('detail-modal').classList.add('open');
      await refreshDetailView(item.id);
    }

    let detailLoadSequence = 0;
    async function refreshDetailView(imageId) {
      closeDetailActions();
      const sequence = ++detailLoadSequence;
      const displayedImageId = currentDetailData?.id ?? null;
      state.detailImageId = imageId;
      state.detailIndex = state.items.findIndex(item => item.id === imageId);

      const mediaBox = document.getElementById('detail-media-container');
      const rawUrl = getDetailMediaUrl(imageId);
      const existingImg = typeof mediaBox.querySelector === 'function' ? mediaBox.querySelector('#dt-main-img') : null;
      const alreadyShowingRaw = Boolean(existingImg && existingImg.getAttribute('src') === rawUrl && !showingDecensoredPreview);
      const previewItem = state.detailIndex >= 0 ? state.items[state.detailIndex] : null;
      const earlyPreload = (!alreadyShowingRaw && canDecodeDetailImages() && (!previewItem || previewItem.media_type !== 'video'))
        ? preloadDetailImage(rawUrl)
        : null;

      const r = await fetch(`/api/image/${imageId}`);
      const d = await r.json();
      if (sequence !== detailLoadSequence || !document.getElementById('detail-modal').classList.contains('open')) {
        if (earlyPreload) earlyPreload.then(res => { if (res?.entry) res.entry.used = false; });
        return;
      }
      if (!r.ok) {
        if (earlyPreload) earlyPreload.then(res => { if (res?.entry) res.entry.used = false; });
        closeDetailModal();
        showDeleteNotice(r.status === 404 ? '圖片紀錄已移除，正在更新圖庫清單。' : '圖片詳情載入失敗，請重試。');
        await Promise.allSettled([loadImages(), loadStats(), loadFolders(), loadTags()]);
        return;
      }

      let preload = null;
      if (!d.file_missing && d.media_type !== 'video' && !alreadyShowingRaw && canDecodeDetailImages()) {
        preload = await (earlyPreload || preloadDetailImage(rawUrl));
        if (sequence !== detailLoadSequence || !document.getElementById('detail-modal').classList.contains('open')) {
          if (preload?.entry) preload.entry.used = false;
          return;
        }
      } else if (earlyPreload) {
        earlyPreload.then(res => { if (res?.entry) res.entry.used = false; });
      }

      currentDetailData = d;
      currentDecensorPreviewUrl = null;
      showingDecensoredPreview = false;

      if (displayedImageId !== d.id) {
        const revBox = document.getElementById('dt-rev-links');
        if (revBox) revBox.style.display = 'none';
      }
      const decBox = document.getElementById('dt-decensor-box');
      if (decBox) decBox.style.display = 'none';
      const cmpBtn = document.getElementById('btn-decensor-compare');
      if (cmpBtn) cmpBtn.style.display = 'none';
      const saveRow = document.getElementById('dt-decensor-save-row');
      if (saveRow) saveRow.style.display = 'none';

      if (d.file_missing) {
        showMissingDetailMedia(d.id);
      } else if (d.media_type === 'video') {
        mediaBox.innerHTML = `<video src="${rawUrl}" controls autoplay loop onerror="showMissingDetailMedia(${d.id})"></video>`;
      } else if (canDecodeDetailImages() && preload && !preload.ok) {
        showMissingDetailMedia(d.id);
      } else {
        const currentImg = typeof mediaBox.querySelector === 'function' ? mediaBox.querySelector('#dt-main-img') : null;
        if (currentImg && currentImg.getAttribute('src') === rawUrl) {
          currentImg.alt = d.filename;
        } else if (typeof mediaBox.replaceChildren === 'function' && preload?.img && typeof Node !== 'undefined' && preload.img instanceof Node) {
          preload.img.id = 'dt-main-img';
          preload.img.alt = d.filename;
          preload.img.draggable = false;
          preload.img.onerror = () => showMissingDetailMedia(d.id);
          mediaBox.replaceChildren(preload.img);
        } else {
          mediaBox.innerHTML = `<img id="dt-main-img" src="${rawUrl}" alt="${escapeHtml(d.filename)}" onerror="showMissingDetailMedia(${d.id})" />`;
        }
      }

      document.getElementById('dt-filename').textContent = d.filename;
      document.getElementById('dt-folder').textContent = d.folder || '根目錄 (D:\\圖檔\\twitter)';
      document.getElementById('dt-size').textContent = d.size_kb.toLocaleString();

      const sugBox = document.getElementById('dt-suggest-box');
      if (d.suggested_folder && !d.folder) {
        sugBox.style.display = 'block';
        document.getElementById('dt-suggest-name').textContent = d.suggested_folder;
      } else {
        sugBox.style.display = 'none';
      }

      renderDetailTags(d.tags);
      preloadAdjacentDetailImages(state.detailIndex);
    }

    function showMissingDetailMedia(imageId) {
      if (state.detailImageId !== imageId) return;
      document.getElementById('detail-media-container').textContent = '圖片檔案不存在或無法讀取。可按「刪除檔案」同步移除失效紀錄與標籤連結。';
    }

    async function openOrRunSingleDecensor() {
      if (!currentDetailData) return;
      if (currentDetailData.media_type === 'video') {
        return alert('目前去馬賽克模型僅支援靜態圖片 (JPG / PNG / WEBP)！');
      }
      const decBox = document.getElementById('dt-decensor-box');
      if (decBox) decBox.style.display = 'flex';
      await previewSingleDecensor();
    }

    async function previewSingleDecensor() {
      if (!currentDetailData) return;
      const btn = document.getElementById('btn-decensor-preview');
      const mainBtn = document.getElementById('btn-single-decensor');
      const mainLabel = document.getElementById('dt-decensor-label');
      const statusEl = document.getElementById('dt-decensor-status');
      const cmpBtn = document.getElementById('btn-decensor-compare');
      const saveRow = document.getElementById('dt-decensor-save-row');
      const mode = document.getElementById('dt-decensor-mode') ? document.getElementById('dt-decensor-mode').value : 'auto';

      if (btn) { btn.disabled = true; btn.textContent = '⏳ GPU 重繪中...'; }
      if (mainBtn) { mainBtn.disabled = true; mainLabel.textContent = '去馬賽克中…'; }
      if (statusEl) statusEl.innerHTML = '⏳ 正在透過 YOLO 遮罩偵測與本機 GGUF 影像修復模型進行局部重繪...';

      try {
        const r = await fetch(`/api/decensor/preview/${currentDetailData.id}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode, conf_threshold: 0.25, expand_ratio: 0.18 })
        });
        const res = await r.json();
        if (!r.ok || !res.ok) {
          if (statusEl) statusEl.innerHTML = `<span style="color:#fca5a5;">⚠️ ${escapeHtml(res.detail || res.message || '未偵測到馬賽克區域')}</span>`;
          if (cmpBtn) cmpBtn.style.display = 'none';
          if (saveRow) saveRow.style.display = 'none';
          return;
        }
        currentDecensorPreviewUrl = res.preview_data_url;
        showingDecensoredPreview = true;
        const imgEl = document.getElementById('dt-main-img');
        if (imgEl && currentDecensorPreviewUrl) {
          imgEl.src = currentDecensorPreviewUrl;
        }
        if (statusEl) {
          statusEl.innerHTML = `<span style="color:#6ee7b7; font-weight:700;">${escapeHtml(res.message)}</span><br><span style="color:#cbd5e1; font-size:11px;">💡 左側大圖已切換為去碼預覽，可點擊「👁️ 切換對比」比較前後差異，滿意後再選擇「另存新檔」或「覆蓋原圖」。</span>`;
        }
        if (cmpBtn) {
          cmpBtn.style.display = 'inline-flex';
          cmpBtn.textContent = '👁️ 切換對比：目前顯示【去碼後】';
        }
        if (saveRow) {
          saveRow.style.display = 'flex';
        }
      } catch (e) {
        if (statusEl) statusEl.innerHTML = `<span style="color:#fca5a5;">❌ 預覽失敗：${escapeHtml(e.message)}</span>`;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔍 重新偵測並預覽'; }
        if (mainBtn) { mainBtn.disabled = false; mainLabel.textContent = '去馬賽克'; }
      }
    }

    function toggleDecensorCompare() {
      if (!currentDetailData || !currentDecensorPreviewUrl) return;
      const imgEl = document.getElementById('dt-main-img');
      const cmpBtn = document.getElementById('btn-decensor-compare');
      if (!imgEl) return;
      showingDecensoredPreview = !showingDecensoredPreview;
      if (showingDecensoredPreview) {
        imgEl.src = currentDecensorPreviewUrl;
        if (cmpBtn) cmpBtn.textContent = '👁️ 切換對比：目前顯示【去碼後】';
      } else {
        imgEl.src = getDetailMediaUrl(currentDetailData.id);
        if (cmpBtn) cmpBtn.textContent = '👁️ 切換對比：目前顯示【原始有碼圖】';
      }
    }

    async function applySingleDecensor(saveMode = 'new_copy') {
      if (!currentDetailData) return;
      if (saveMode === 'overwrite') {
        if (!confirm('確定要直接覆蓋原始圖片檔案嗎？（建議可先選擇「另存無碼新檔」保留原圖）')) return;
      }
      const statusEl = document.getElementById('dt-decensor-status');
      const mode = document.getElementById('dt-decensor-mode') ? document.getElementById('dt-decensor-mode').value : 'auto';
      if (statusEl) statusEl.innerHTML = '⏳ 正在輸出高畫質無碼圖片並更新圖庫索引...';

      try {
        const r = await fetch(`/api/decensor/apply/${currentDetailData.id}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode, save_mode: saveMode, conf_threshold: 0.25, expand_ratio: 0.18 })
        });
        const res = await r.json();
        if (!r.ok || !res.ok) {
          alert(res.detail || res.message || '儲存失敗');
          return;
        }
        const targetId = res.image_id || currentDetailData.id;
        invalidateDetailMediaCache(targetId);
        await refreshDetailView(targetId);
        await loadStats();
        await loadTags();
        await loadImages();
        const decBox = document.getElementById('dt-decensor-box');
        if (decBox) decBox.style.display = 'flex';
        const newStatusEl = document.getElementById('dt-decensor-status');
        if (newStatusEl) {
          newStatusEl.innerHTML = `<span style="color:#34d399; font-weight:700;">${escapeHtml(res.message)}</span>`;
        }
      } catch (e) {
        alert('儲存去碼圖片失敗：' + e.message);
      }
    }

    function renderDetailTags(tags) {
      const groups = [
        { key: 'artist', title: '🎨 畫師 (Artist)' },
        { key: 'copyright', title: '📚 作品 / IP (Copyright)' },
        { key: 'character', title: '👤 角色 (Character)' },
        { key: 'rating', title: '🔞 分級 (Rating)' },
        { key: 'general', title: '🏷️ 內容與服裝屬性 (General)' },
        { key: 'source', title: '📌 來源與年份' }
      ];

      const box = document.getElementById('dt-tags-container');
      let html = '';

      for (const g of groups) {
        const list = tags.filter(t => g.key === 'source' ? (t.category === 'source' || t.category === 'year') : t.category === g.key);
        if (!list.length) continue;

        html += `<div><div class="tag-group-title">${g.title}</div><div>`;
        for (const t of list) {
          const pct = t.source === 'ai' ? ` <span style="color:#94a3b8;font-size:10px;">${Math.round(t.confidence * 100)}%</span>` : '';
          const srcBadge = t.source === 'folder' ? '📁' : (t.source === 'manual' ? '✍️' : (t.source === 'pixiv' ? '🅿️' : ''));
          const display = t.zh_name ? `${t.zh_name} (${t.name})` : t.name;
          html += `
            <span class="detail-tag-chip">
              <span style="cursor:pointer;" onclick="closeDetailModal(); toggleIncludeTag('${escapeJs(t.name)}')">
                ${srcBadge} ${escapeHtml(display)}${pct}
              </span>
              <span class="del-x" onclick="removeSingleTag('${escapeJs(t.name)}')" title="刪除此標籤">×</span>
            </span>
          `;
        }
        html += `</div></div>`;
      }

      if (!html) {
        html = `<div style="color:var(--text-muted); font-size:13px;">此圖片尚無標籤，可從「更多」使用「智慧標籤」自動辨識。</div>`;
      }
      box.innerHTML = html;
    }

    function closeDetailModal() {
      closeDetailActions();
      detailLoadSequence++;
      document.getElementById('detail-modal').classList.remove('open');
      document.getElementById('detail-media-container').innerHTML = '';
      document.getElementById('dt-filename').textContent = '';
      document.getElementById('dt-tags-container').textContent = '';
      currentDetailData = null;
      state.detailImageId = null;
      state.detailIndex = -1;
    }

    function navigateDetail(delta) {
      const nextIdx = state.detailIndex + delta;
      if (nextIdx >= 0 && nextIdx < state.items.length) {
        openDetailModal(nextIdx);
      }
    }

    async function runSingleAiTag() {
      if (!currentDetailData) return;
      const btn = document.getElementById('btn-single-ai');
      const oldText = btn.textContent;
      btn.textContent = '⏳ AI 載入並辨識中...';
      btn.disabled = true;
      try {
        const r = await fetch(`/api/ai/single/${currentDetailData.id}`, { method: 'POST' });
        if (!r.ok) {
          const err = await r.json();
          alert(err.detail || '辨識失敗');
        } else {
          await refreshDetailView(currentDetailData.id);
          await loadStats();
          await loadTags();
          await loadImages();
        }
      } finally {
        btn.textContent = oldText;
        btn.disabled = false;
      }
    }

    async function removeSingleTag(tname) {
      if (!currentDetailData) return;
      await fetch('/api/tags/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_ids: [currentDetailData.id],
          remove_tag_names: [tname]
        })
      });
      await refreshDetailView(currentDetailData.id);
      await loadTags();
      await loadImages();
    }

    async function moveSingleToSuggested() {
      if (!currentDetailData || !currentDetailData.suggested_folder) return;
      await moveSingleToFolder(currentDetailData.suggested_folder);
    }

    async function moveSingleToFolder(folderName) {
      if (!currentDetailData) return;
      await fetch('/api/organize/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_ids: [currentDetailData.id],
          target_folder: folderName,
          mode: 'selected'
        })
      });
      await refreshDetailView(currentDetailData.id);
      await loadStats();
      await loadFolders();
      await loadImages();
    }

    async function openInExplorer() {
      if (!currentDetailData) return;
      try {
        const response = await fetch(`/api/open_explorer/${currentDetailData.id}`, { method: 'POST' });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(typeof data.detail === 'string' ? data.detail : '無法開啟圖片所在的資料夾');
        }
      } catch (error) {
        alert(`開啟資料夾失敗：${error.message}`);
      }
    }

    /* Smart Tagging Modal, Model Load/Unload & Polling */
    function openAiModal() {
      const base = function () {
      document.getElementById('ai-modal').classList.add('open');
      checkAiStatus();
      };
      return window.GalleryApp ? window.GalleryApp.run("openAiModal", Array.from(arguments), base) : base();
    }
    function closeAiModal() {
      const base = function () { document.getElementById('ai-modal').classList.remove('open'); 
      };
      return window.GalleryApp ? window.GalleryApp.run("closeAiModal", Array.from(arguments), base) : base();
    }
    function openReverseSearchModal() { openAiModal(); }
    function closeReverseSearchModal() { closeAiModal(); }

    function syncModelUiState(stAi) {
      const base = function (stAi) {
      if (!stAi) return;
      const topBtn = document.getElementById('btn-top-unload-model');
      if (topBtn) {
        topBtn.style.display = stAi.model_loaded ? 'inline-flex' : 'none';
      }
      const stateEl = document.getElementById('ai-modal-model-state');
      const loadBtn = document.getElementById('btn-modal-load-vram');
      const vramBtn = document.getElementById('btn-modal-unload-vram');
      const diskBtn = document.getElementById('btn-modal-unload-disk');
      const startBtn = document.getElementById('btn-start-smart-tag');
      if (stateEl) {
        if (stAi.model_loaded) {
          stateEl.innerHTML = `<span style="color:#34d399;">🟢 已載入於顯卡記憶體 (${escapeHtml(stAi.provider || 'GPU')})</span>`;
        } else if (stAi.running && (stAi.status_message || '').includes('載入')) {
          stateEl.innerHTML = `<span style="color:#fcd34d;">⏳ 正在載入模型至 GPU 顯卡記憶體...</span>`;
        } else if (stAi.disk_cached) {
          stateEl.innerHTML = `<span style="color:#94a3b8;">⚪ 未載入記憶體 (VRAM 0 MB 佔用，硬碟快取 ${stAi.disk_size_mb || 750} MB)</span>`;
        } else {
          stateEl.innerHTML = `<span style="color:#94a3b8;">⚪ 未載入且硬碟無快取檔 (0 MB 佔用)</span>`;
        }
      }
      if (loadBtn) loadBtn.disabled = !!stAi.model_loaded || !!stAi.running;
      if (vramBtn) vramBtn.disabled = !stAi.model_loaded;
      if (diskBtn) diskBtn.disabled = !stAi.model_loaded && !stAi.disk_cached;
      if (startBtn) {
        startBtn.disabled = !!stAi.running;
        startBtn.textContent = stAi.running
          ? '⏳ 智慧標籤執行中...'
          : '🚀 啟動智慧標籤 (AI 打標 ➔ 搜圖校正 ➔ 建夾歸檔)';
      }
      };
      return window.GalleryApp ? window.GalleryApp.run("syncModelUiState", Array.from(arguments), base) : base(stAi);
    }

    async function loadAiModelNow() {
      const base = async function () {
      const loadBtn = document.getElementById('btn-modal-load-vram');
      const stateEl = document.getElementById('ai-modal-model-state');
      if (loadBtn) {
        loadBtn.disabled = true;
        loadBtn.textContent = '⏳ 載入中...';
      }
      if (stateEl) {
        stateEl.innerHTML = `<span style="color:#fcd34d;">⏳ 正在將 Camie Tagger v2 載入至 GPU 顯卡記憶體...</span>`;
      }
      try {
        const r = await fetch('/api/ai/load', { method: 'POST' });
        const d = await r.json();
        await checkAiStatus();
      } catch (e) {
        alert('載入模型失敗：' + e.message);
      } finally {
        if (loadBtn) {
          loadBtn.textContent = '⚡ 載入模型至 GPU';
        }
      }
      };
      return window.GalleryApp ? window.GalleryApp.run("loadAiModelNow", Array.from(arguments), base) : base();
    }

    async function unloadAiModel(deleteDiskFiles = false) {
      const base = async function (deleteDiskFiles = false) {
      if (deleteDiskFiles) {
        if (!confirm('確定要卸載 AI 模型，並同時刪除硬碟上的 .onnx 模型快取檔（約 750MB）嗎？下次執行 AI 打標時會重新下載。')) return;
      }
      const r = await fetch(`/api/ai/unload?delete_disk_files=${deleteDiskFiles}`, { method: 'POST' });
      const d = await r.json();
      alert(`✅ ${d.message || '已成功卸載 AI 模型並釋放記憶體！'}`);
      await checkAiStatus();
      };
      return window.GalleryApp ? window.GalleryApp.run("unloadAiModel", Array.from(arguments), base) : base(deleteDiskFiles);
    }

    async function startAiBatch() {
      const base = async function () {
      const scope = document.getElementById('ai-scope').value;
      const limit = document.getElementById('ai-limit').value;
      const genT = document.getElementById('ai-gen-thresh').value;
      const artT = document.getElementById('ai-art-thresh').value;
      const runLocalAi = document.getElementById('ai-step-local') ? document.getElementById('ai-step-local').checked : true;
      const runRevCalibrate = document.getElementById('ai-step-rev') ? document.getElementById('ai-step-rev').checked : true;
      const revLimit = document.getElementById('rev-limit') ? document.getElementById('rev-limit').value : '30';
      const revSim = document.getElementById('rev-sim') ? document.getElementById('rev-sim').value : '72';
      const revApiKey = document.getElementById('rev-apikey') ? document.getElementById('rev-apikey').value.trim() : '';
      const runSemThemes = false;
      const autoMove = document.getElementById('ai-auto-move') ? document.getElementById('ai-auto-move').checked : false;
      const runDecensor = document.getElementById('ai-step-decensor') ? document.getElementById('ai-step-decensor').checked : false;
      const decensorSaveMode = document.getElementById('ai-decensor-save-mode') ? document.getElementById('ai-decensor-save-mode').value : 'new_copy';
      const autoUnload = document.getElementById('ai-auto-unload') ? document.getElementById('ai-auto-unload').checked : true;

      if (!runLocalAi && !runRevCalibrate && !runSemThemes && !runDecensor) {
        return alert('請至少勾選一個執行階段！');
      }

      // 立即在視窗內顯示進度面板與載入狀態，讓使用者清楚看到模型載入與即時進度
      const liveBox = document.getElementById('ai-modal-live-box');
      if (liveBox) {
        liveBox.style.display = 'flex';
        document.getElementById('ai-modal-live-msg').textContent = runLocalAi
          ? '⏳ 正在載入本機 AI 視覺模型至 GPU 顯卡記憶體並啟動任務...'
          : '⏳ 正在啟動智慧標籤任務...';
        document.getElementById('ai-modal-live-pct').textContent = '0%';
        document.getElementById('ai-modal-live-bar').style.width = '2%';
      }

      const params = new URLSearchParams({
        scope,
        limit,
        general_thresh: genT,
        artist_thresh: artT,
        run_local_ai: runLocalAi,
        run_reverse_search_calibrate: runRevCalibrate,
        reverse_search_limit: revLimit,
        min_similarity: revSim,
        run_semantic_themes: runSemThemes,
        auto_move_to_artist_folder: autoMove,
        run_decensor: runDecensor,
        decensor_save_mode: decensorSaveMode,
        auto_unload: autoUnload
      });
      if (revApiKey) {
        params.set('saucenao_api_key', revApiKey);
      }

      const response = await fetch(`/api/ai/start?${params.toString()}`, { method: 'POST' });
      const started = await response.json();
      if (!response.ok || started.status === 'already_running') {
        alert(started.message || '背景任務正在執行，請稍後重試');
        return;
      }
      await checkAiStatus();
      };
      return window.GalleryApp ? window.GalleryApp.run("startAiBatch", Array.from(arguments), base) : base();
    }

    async function stopAiBatch() {
      const base = async function () {
      const active=window.GalleryApp?.runtime.value?.active_job;
      if (active?.id) {
        await GalleryApp.api(`/api/jobs/${encodeURIComponent(active.id)}/stop`, { method: 'POST' });
        await checkAiStatus();
        return;
      }
      await fetch('/api/ai/stop', { method: 'POST' });
      await fetch('/api/reverse_search/stop', { method: 'POST' });
      checkAiStatus();
      };
      return window.GalleryApp ? window.GalleryApp.run("stopAiBatch", Array.from(arguments), base) : base();
    }

    let pollGeneration = 0;
    let visibilityPollingBound = false;
    function startAiPolling() {
      const generation = ++pollGeneration;
      clearTimeout(aiPollTimer);
      const poll = async () => {
        await checkAiStatus();
        if (generation !== pollGeneration) return;
        const runtime = window.GalleryApp?.runtime.value;
        const busy = runtime?.active_job?.id || runtime?.model?.busy;
        aiPollTimer = setTimeout(poll, document.hidden ? 30000 : busy ? 1000 : 10000);
      };
      void poll();
      if (!visibilityPollingBound) {
        visibilityPollingBound = true;
        document.addEventListener('visibilitychange', () => { if (!document.hidden) startAiPolling(); });
      }
    }

    let wasAiRunning = false;
    let bannerHideTimeout = null;

    async function checkAiStatus() {
      try {
        const runtime = window.GalleryApp ? await GalleryApp.refreshRuntime() : null;
        let stAi, stRev, stSch;
        if (runtime) ({ ai: stAi, reverse: stRev, schedule: stSch } = runtime);
        else {
          const responses = await Promise.all([fetch('/api/ai/status'), fetch('/api/reverse_search/status'), fetch('/api/schedule/status')]);
          [stAi, stRev, stSch] = await Promise.all(responses.map(response => response.json()));
        }
        const externalJob = runtime?.active_job?.id ? {
          running: true, status_message: `${runtime.active_job.name} · ${runtime.active_job.stage || '處理中'}`,
          processed_count: runtime.active_job.processed_count || 0, total_target: runtime.active_job.total_target || 0,
          current_file: runtime.active_job.current_file || '', external_process: runtime.active_job.pid !== runtime.process_id
          , can_stop: runtime.active_job.can_stop !== false
        } : null;

        syncModelUiState(stAi);

        const schLiveEl = document.getElementById('sch-live-status');
        if (schLiveEl && stSch) {
          schLiveEl.textContent = stSch.status_message || '待機中';
          if (document.getElementById('schedule-modal').classList.contains('open')) renderScheduleHistory(stSch);
        }

        const schAsBanner = stSch.running_job ? {
          running: true,
          status_message: `正在執行排程自動處理 — 整體進度：${stSch.processed_count || 0}%`,
          total_target: stSch.total_target || 100,
          processed_count: stSch.processed_count || 0
        } : null;

        const isAnyRunning = !!(externalJob || stAi.running || stRev.running || stSch.running_job || runtime?.model?.busy);

        if (isAnyRunning) {
          if (bannerHideTimeout) {
            clearTimeout(bannerHideTimeout);
            bannerHideTimeout = null;
          }
          wasAiRunning = true;
          const activeSt = externalJob || schAsBanner || (stRev.running ? stRev : stAi);
          updateAiBanner(activeSt, true);
          // Only update top header numbers quietly without rebuilding sidebar DOM
          if (!runtime || Date.now() - (window.GalleryApp.runtime.lastStatsAt || 0) > 5000) { if (runtime) GalleryApp.runtime.lastStatsAt = Date.now(); void loadStats(); }
        } else if (wasAiRunning) {
          wasAiRunning = false;
          const lastJob = runtime?.jobs?.[0];
          const failed = lastJob && ['failed', 'interrupted', 'cancelled'].includes(lastJob.status);
          const finishedSt = failed ? { status_message: `${lastJob.name}：${lastJob.last_error || (lastJob.status === 'cancelled' ? '已停止' : '未完成，請至設定查看')}` } : stRev.status_message && stRev.status_message.includes('完成') ? stRev : stAi;
          updateAiBanner({
            running: false,
            status_message: finishedSt.status_message || '✅ 整體處理任務已完成！',
            terminal_state: failed ? lastJob.status : 'succeeded',
            total_target: 100,
            processed_count: failed ? 0 : 100,
            speed_img_per_sec: stAi.speed_img_per_sec || 0,
            current_file: '已完成'
          }, false);
          await loadStats();
          await loadFolders();
          await loadTags();
          await loadImages();
          bannerHideTimeout = setTimeout(() => {
            document.getElementById('ai-banner').classList.remove('active');
          }, 5000);
        }
      } catch (e) { if (window.GalleryApp) { GalleryApp.runtime.error = e.message; GalleryApp.emit('runtime:error', e.message); } }
    }

    function updateAiBanner(st, isRunning = true) {
      const base = function (st, isRunning = true) {
      if (!st) return;
      const banner = document.getElementById('ai-banner');
      const liveBox = document.getElementById('ai-modal-live-box');
      const stopBtn = document.getElementById('btn-modal-stop-task');
      if (isRunning || st.running) {
        banner.classList.add('active');
        const total = st.total_target > 0 ? st.total_target : 100;
        const done = st.processed_count || 0;
        const pct = Math.min(100, Math.max(0, Math.round((done / total) * 100)));
        const msg = st.status_message || `正在執行處理任務 — 整體進度：${done.toLocaleString()} / ${total.toLocaleString()}`;
        document.getElementById('ai-banner-msg').textContent = msg;
        document.getElementById('ai-progress-bar').style.width = `${pct}%`;
        document.getElementById('ai-banner-pct').textContent = `${pct}%`;

        if (liveBox) {
          liveBox.style.display = 'flex';
          document.getElementById('ai-modal-live-msg').textContent = msg;
          document.getElementById('ai-modal-live-bar').style.width = `${Math.max(2, pct)}%`;
          document.getElementById('ai-modal-live-pct').textContent = `${pct}% (${done.toLocaleString()} / ${total.toLocaleString()})`;
          document.getElementById('ai-modal-live-file').textContent = `目前處理檔案：${st.current_file || '準備中...'}`;
          document.getElementById('ai-modal-live-speed').textContent = `處理速度：${st.speed_img_per_sec || 0} 張/秒`;
          if (stopBtn) stopBtn.style.display = 'inline-flex';
        }
      } else if (st.processed_count === 100) {
        banner.classList.add('active');
        document.getElementById('ai-banner-msg').textContent = st.status_message;
        document.getElementById('ai-progress-bar').style.width = '100%';
        document.getElementById('ai-banner-pct').textContent = '100%';

        if (liveBox) {
          liveBox.style.display = 'flex';
          document.getElementById('ai-modal-live-msg').textContent = st.status_message;
          document.getElementById('ai-modal-live-bar').style.width = '100%';
          document.getElementById('ai-modal-live-pct').textContent = '100%';
          document.getElementById('ai-modal-live-file').textContent = '目前處理檔案：已完成';
          if (stopBtn) stopBtn.style.display = 'none';
        }
      } else if (st.terminal_state && st.terminal_state !== 'succeeded') {
        banner.classList.add('active');
        document.getElementById('ai-banner-msg').textContent = st.status_message;
        document.getElementById('ai-banner-pct').textContent = '未完成';
        document.getElementById('ai-progress-bar').style.width = '0%';
        if (stopBtn) stopBtn.style.display = 'none';
      } else {
        banner.classList.remove('active');
      }
      };
      return window.GalleryApp ? window.GalleryApp.run("updateAiBanner", Array.from(arguments), base) : base(st, isRunning);
    }

    /* Organize & Artist Folder Auto-Creation Utilities */
    let currentOrgPreviewFolders = [];

    function openOrganizeModal() {
      document.getElementById('organize-modal').classList.add('open');
      loadArtistFoldersPreview();
    }

    function closeOrganizeModal() {
      document.getElementById('organize-modal').classList.remove('open');
    }

    async function loadArtistFoldersPreview() {
      const minConf = document.getElementById('org-min-conf') ? document.getElementById('org-min-conf').value : '0.20';
      const minImgs = document.getElementById('org-min-imgs') ? document.getElementById('org-min-imgs').value : '1';
      const onlyEx = document.getElementById('org-only-existing') ? document.getElementById('org-only-existing').checked : false;

      const box = document.getElementById('org-preview-box');
      const sumBar = document.getElementById('org-summary-bar');
      if (box) box.innerHTML = '⏳ 正在分析根目錄已辨識繪師與目標資料夾...';

      const params = new URLSearchParams({
        min_confidence: minConf,
        min_images_per_artist: minImgs,
        only_existing_folders: onlyEx
      });
      const r = await fetch(`/api/organize/artist_folders_preview?${params.toString()}`);
      const d = await r.json();
      currentOrgPreviewFolders = d.folders || [];

      if (sumBar) {
        sumBar.innerHTML = `
          <span>🎯 可立即歸檔：<b>${d.total_ready_images}</b> 張圖片 ➔ 共 <b>${d.total_artist_folders}</b> 個繪師資料夾（➕ 新建 <b style="color:#fcd34d;">${d.new_folders_to_create}</b> 個 / 📁 併入既有 <b>${d.existing_folders_to_update}</b> 個）</span>
          <span style="color:var(--text-muted); font-size:12px;">根目錄尚有 ${d.untagged_root_remaining.toLocaleString()} 張未打標圖片</span>
        `;
      }

      if (!currentOrgPreviewFolders.length) {
        box.innerHTML = `<div style="padding:20px; color:var(--text-muted);">目前沒有符合條件的待歸檔繪師圖片。你可以調低門檻，或點擊下方「🔍 對未標籤圖啟動以圖搜圖＋建夾」/「🤖 AI 打標＋建夾」！</div>`;
        return;
      }

      let html = `<table style="width:100%; border-collapse:collapse; text-align:left;">
        <thead>
          <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
            <th style="padding:6px;">目標繪師資料夾 (D:\\圖檔\\twitter\\...)</th>
            <th style="padding:6px;">狀態</th>
            <th style="padding:6px;">張數</th>
            <th style="padding:6px;">來源 / 信心度</th>
            <th style="padding:6px;">範例檔案</th>
            <th style="padding:6px; text-align:right;">單獨操作</th>
          </tr>
        </thead><tbody>`;

      for (let idx = 0; idx < currentOrgPreviewFolders.length; idx++) {
        const f = currentOrgPreviewFolders[idx];
        const statusBadge = f.is_new_folder
          ? `<span style="background:rgba(245,158,11,0.18); color:#fcd34d; border:1px solid #f59e0b; padding:1px 6px; border-radius:4px; font-size:11px;">➕ 新建繪師資料夾</span>`
          : `<span style="background:rgba(16,185,129,0.18); color:#6ee7b7; border:1px solid #10b981; padding:1px 6px; border-radius:4px; font-size:11px;">📁 併入既有資料夾</span>`;
        const srcText = (f.sources || []).join(', ');
        const samples = (f.sample_filenames || []).slice(0, 2).join(', ');
        html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
          <td style="padding:6px; font-weight:700; color:#f8fafc;">📁 ${escapeHtml(f.folder_name)}</td>
          <td style="padding:6px;">${statusBadge}</td>
          <td style="padding:6px; font-weight:700; color:#34d399;">${f.image_count} 張</td>
          <td style="padding:6px; color:#a5b4fc;">${escapeHtml(srcText)} (${Math.round(f.avg_confidence * 100)}%)</td>
          <td style="padding:6px; color:var(--text-muted); max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml((f.sample_filenames || []).join('\n'))}">${escapeHtml(samples)}</td>
          <td style="padding:6px; text-align:right;">
            <button class="btn btn-primary" style="padding:2px 8px; font-size:11px;" onclick="moveSingleArtistGroup(${idx})">建夾並移入</button>
          </td>
        </tr>`;
      }
      html += `</tbody></table>`;
      box.innerHTML = html;
    }

    async function moveSingleArtistGroup(idx) {
      const f = currentOrgPreviewFolders[idx];
      if (!f) return;
      const autoRename = document.getElementById('org-auto-rename') ? document.getElementById('org-auto-rename').checked : true;
      const r = await fetch('/api/organize/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: 'selected',
          image_ids: f.image_ids,
          target_folder: f.folder_name,
          auto_rename_on_move: autoRename
        })
      });
      const d = await r.json();
      await loadArtistFoldersPreview();
      await loadStats();
      await loadFolders();
      await loadImages();
    }

    async function organizeAiSuggested() {
      const minConf = parseFloat(document.getElementById('org-min-conf') ? document.getElementById('org-min-conf').value : '0.20');
      const minImgs = parseInt(document.getElementById('org-min-imgs') ? document.getElementById('org-min-imgs').value : '1', 10);
      const onlyEx = document.getElementById('org-only-existing') ? document.getElementById('org-only-existing').checked : false;
      const autoRename = document.getElementById('org-auto-rename') ? document.getElementById('org-auto-rename').checked : true;

      const btn = document.getElementById('btn-org-exec');
      const oldText = btn ? btn.textContent : '';
      if (btn) {
        btn.textContent = '⏳ 正在建立繪師資料夾並搬移圖片...';
        btn.disabled = true;
      }
      try {
        const r = await fetch('/api/organize/move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mode: 'auto_artist_folders',
            min_confidence: minConf,
            min_images_per_artist: minImgs,
            only_existing_folders: onlyEx,
            auto_rename_on_move: autoRename
          })
        });
        const d = await r.json();
        alert(`✅ 歸檔完成！\n• 共搬移歸檔：${d.moved} 張圖片\n• 新建繪師資料夾：${d.new_folders_created || 0} 個\n• 併入既有繪師資料夾：${d.existing_folders_updated || 0} 個`);
        await loadArtistFoldersPreview();
        await loadStats();
        await loadFolders();
        await loadImages();
      } finally {
        if (btn) {
          btn.textContent = oldText;
          btn.disabled = false;
        }
      }
    }

    async function organizeByDate() {
      if (!confirm('確定要將根目錄中所有 YYYYMMDD_NNNNN 格式的圖片，自動依「年-月 (YYYY-MM)」整理至 D:\\圖檔\\twitter\\_按日期歸檔\\ 嗎？')) return;
      const r = await fetch('/api/organize/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'by_date_ym' })
      });
      const d = await r.json();
      alert(`已成功將 ${d.moved} 張圖片按「年-月」歸檔至 _按日期歸檔 資料夾！`);
      closeOrganizeModal();
      await loadStats();
      await loadFolders();
      await loadImages();
    }

    async function runPixivLookup() {
      const r = await fetch('/api/pixiv/lookup?limit=80', { method: 'POST' });
      const d = await r.json();
      alert(`Pixiv ID 反查完成！共檢查 ${d.checked_illusts || 0} 件作品，成功取回 ${d.found_illusts || 0} 位真實畫師與原圖標籤。`);
      await loadStats();
      await loadTags();
      await loadImages();
    }

    async function rescanLibrary() {
      const r = await fetch('/api/scan', { method: 'POST' });
      const d = await r.json();
      alert(`掃描完成！總圖數：${d.total_images} (新增 ${d.new_added} 張，耗時 ${d.elapsed_sec} 秒)`);
      await loadStats();
      await loadFolders();
      await loadTags();
      await loadImages();
    }

    /* Batch Rename & Order Lock Functions */
    let rnTimer = null;

    function openRenameModal() {
      const sel = document.getElementById('rn-folder');
      let opts = `<option value="__ALL_ARTIST_FOLDERS__">🌟 全部繪師子資料夾一鍵統一格式 (53 個資料夾)</option>`;
      opts += `<option value="__ROOT__">🚨 根目錄未整理檔案 (YYYYMMDD_00001.jpg)</option>`;
      for (const f of state.folders) {
        const selectedAttr = (state.folder === f.name) ? 'selected' : '';
        opts += `<option value="${escapeHtml(f.name)}" ${selectedAttr}>📁 ${escapeHtml(f.name)} (${f.count} 張)</option>`;
      }
      sel.innerHTML = opts;
      if (state.folder && state.folder !== '__ALL__' && state.folder !== '__FOLDERS__' && state.folder !== '__SUGGESTED__') {
        sel.value = state.folder;
      }
      document.getElementById('rn-date').value = '';
      document.getElementById('rn-artist').value = '';
      document.getElementById('rename-modal').classList.add('open');
      loadRenamePreview();
    }

    function closeRenameModal() {
      document.getElementById('rename-modal').classList.remove('open');
    }

    function debounceRenamePreview() {
      clearTimeout(rnTimer);
      rnTimer = setTimeout(loadRenamePreview, 250);
    }

    async function loadRenamePreview() {
      const folder = document.getElementById('rn-folder').value;
      const pad = document.getElementById('rn-pad').value;
      const cdate = document.getElementById('rn-date').value.trim();
      const cartist = document.getElementById('rn-artist').value.trim();

      const box = document.getElementById('rn-preview-box');
      const sumBar = document.getElementById('rn-summary-bar');
      document.getElementById('btn-rn-exec').style.display = 'inline-flex';
      box.innerHTML = '⏳ 正在依 Windows 自然排序計算新舊檔名對照表...';

      const params = new URLSearchParams({ folder, pad_digits: pad });
      if (cdate) params.set('custom_date', cdate);
      if (cartist) params.set('custom_artist', cartist);

      const r = await fetch(`/api/rename/preview?${params.toString()}`);
      const d = await r.json();

      if (d.mode === 'all_folders') {
        sumBar.textContent = `📊 全部 ${d.folder_count} 個繪師資料夾：共 ${d.total.toLocaleString()} 張圖片，預計將標準化改名 ${d.changed_count.toLocaleString()} 張`;
        let html = `<table style="width:100%; border-collapse:collapse; text-align:left;">
          <thead>
            <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
              <th style="padding:6px;">資料夾名稱</th>
              <th style="padding:6px;">總數 (需調整)</th>
              <th style="padding:6px;">第 1 張 (舊 ➔ 新)</th>
              <th style="padding:6px;">最後 1 張 (舊 ➔ 新)</th>
            </tr>
          </thead><tbody>`;
        for (const f of d.folders) {
          html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="padding:6px; font-weight:700; color:#fcd34d;">📁 ${escapeHtml(f.folder)}</td>
            <td style="padding:6px;">${f.total} (<span style="color:#34d399;">${f.changed_count}</span>)</td>
            <td style="padding:6px;"><span style="color:#94a3b8;">${escapeHtml(f.sample_first_old)}</span><br>➔ <span style="color:#6ee7b7;">${escapeHtml(f.sample_first_new)}</span></td>
            <td style="padding:6px;"><span style="color:#94a3b8;">${escapeHtml(f.sample_last_old)}</span><br>➔ <span style="color:#6ee7b7;">${escapeHtml(f.sample_last_new)}</span></td>
          </tr>`;
        }
        html += `</tbody></table>`;
        box.innerHTML = html;
      } else {
        sumBar.textContent = `📊 資料夾 [${d.folder}] (基準日期: ${d.base_date})：共 ${d.total.toLocaleString()} 張圖片，預計需要改名 ${d.changed_count.toLocaleString()} 張 (前 300 筆預覽)`;
        let html = `<table style="width:100%; border-collapse:collapse; text-align:left;">
          <thead>
            <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
              <th style="padding:5px; width:60px;">順序</th>
              <th style="padding:5px;">目前檔名 (Windows 自然排序)</th>
              <th style="padding:5px; width:30px;"></th>
              <th style="padding:5px;">改名後新檔名 (鎖定順序)</th>
            </tr>
          </thead><tbody>`;
        for (const it of d.items) {
          const colorNew = it.changed ? '#6ee7b7' : '#94a3b8';
          const statusBadge = it.changed ? '' : ' <span style="font-size:10px;color:#64748b;">(已符合)</span>';
          html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
            <td style="padding:4px 6px; color:#64748b;">#${it.seq}</td>
            <td style="padding:4px 6px; color:#cbd5e1;">${escapeHtml(it.old_filename)}</td>
            <td style="padding:4px 6px;">➔</td>
            <td style="padding:4px 6px; color:${colorNew}; font-weight:${it.changed ? '600' : '400'};">${escapeHtml(it.new_filename)}${statusBadge}</td>
          </tr>`;
        }
        html += `</tbody></table>`;
        box.innerHTML = html;
      }
    }

    async function executeRename() {
      const folder = document.getElementById('rn-folder').value;
      const pad = parseInt(document.getElementById('rn-pad').value, 10);
      const cdate = document.getElementById('rn-date').value.trim() || null;
      const cartist = document.getElementById('rn-artist').value.trim() || null;

      const btn = document.getElementById('btn-rn-exec');
      const oldText = btn.textContent;
      btn.textContent = '⏳ 正在執行兩階段安全改名...';
      btn.disabled = true;

      try {
        const r = await fetch('/api/rename/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            folder,
            pad_digits: pad,
            custom_date: cdate,
            custom_artist: cartist
          })
        });
        const d = await r.json();
        if (!r.ok) {
          alert('改名失敗: ' + (d.detail || JSON.stringify(d)));
        } else {
          alert(`✅ 改名完成！共成功更新 ${d.renamed || 0} 個檔案（已自動建立還原備份紀錄）。`);
          await loadRenamePreview();
          await loadImages();
        }
      } finally {
        btn.textContent = oldText;
        btn.disabled = false;
      }
    }

    async function loadRenameLogsView() {
      const r = await fetch('/api/rename/logs');
      const logs = await r.json();
      const box = document.getElementById('rn-preview-box');
      const sumBar = document.getElementById('rn-summary-bar');
      document.getElementById('btn-rn-exec').style.display = 'none';
      sumBar.textContent = `⏪ 歷史改名紀錄（點擊「一鍵還原」即可恢復該批次的原始檔名）`;

      if (!logs.length) {
        box.innerHTML = `<div style="padding:20px; color:var(--text-muted);">目前尚無改名紀錄。</div>`;
        return;
      }

      let html = `<table style="width:100%; border-collapse:collapse; text-align:left;">
        <thead>
          <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
            <th style="padding:6px;">執行時間</th>
            <th style="padding:6px;">目標資料夾</th>
            <th style="padding:6px;">改名數量</th>
            <th style="padding:6px;">操作</th>
          </tr>
        </thead><tbody>`;
      for (const lg of logs) {
        html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
          <td style="padding:6px;">${escapeHtml(lg.timestamp)}</td>
          <td style="padding:6px; font-weight:700; color:#fcd34d;">${escapeHtml(lg.folder)}</td>
          <td style="padding:6px;">${lg.count} 個檔案</td>
          <td style="padding:6px;">
            <button class="btn btn-warn" style="padding:3px 10px; font-size:12px;" onclick="undoRenameBatch('${escapeJs(lg.batch_id)}')">⏪ 一鍵還原舊檔名</button>
          </td>
        </tr>`;
      }
      html += `</tbody></table>`;
      box.innerHTML = html;
    }

    async function undoRenameBatch(batchId) {
      if (!confirm('確定要將此批次的檔案全部還原為改名前的原始檔名嗎？')) return;
      const r = await fetch(`/api/rename/undo/${encodeURIComponent(batchId)}`, { method: 'POST' });
      const d = await r.json();
      alert(`✅ 已成功還原 ${d.restored || 0} 個檔案的原始檔名！`);
      await loadRenameLogsView();
      await loadImages();
    }

    /* Semantic Correlation & Gemini MCP Functions */
    function openSemanticModal() {
      document.getElementById('semantic-modal').classList.add('open');
      if (state.includeTags.length > 0) {
        document.getElementById('sem-query-input').value = state.includeTags[0];
        runCorrelationSearch();
      }
    }

    function closeSemanticModal() {
      document.getElementById('semantic-modal').classList.remove('open');
    }

    async function runCorrelationSearch() {
      const q = document.getElementById('sem-query-input').value.trim();
      const box = document.getElementById('sem-result-box');
      if (!q) return alert('請輸入畫師或標籤名稱');
      box.innerHTML = '⏳ 正在計算標籤共現矩陣與 Jaccard 相似度...';

      const r = await fetch(`/api/semantic/correlations?target_tag=${encodeURIComponent(q)}&top_k=40`);
      const d = await r.json();

      if (!d.found) {
        // Fallback to semantic expand search
        const r2 = await fetch(`/api/semantic/expand?q=${encodeURIComponent(q)}`);
        const d2 = await r2.json();
        if (!d2.matched_tags || !d2.matched_tags.length) {
          box.innerHTML = `<div style="color:#f87171;">找不到與「${escapeHtml(q)}」對應的標籤（若尚未執行 AI 打標，請先執行 AI 自動打標）。</div>`;
          return;
        }
        let html = `<div style="margin-bottom:10px; font-weight:700; color:#a5b4fc;">🔍 語意展開匹配到 ${d2.matched_tags.length} 個相關標籤（共 ${d2.total_images_found} 張圖片）：</div><div style="display:flex; flex-wrap:wrap; gap:6px;">`;
        for (const mt of d2.matched_tags) {
          const lb = mt.zh_name ? `${mt.zh_name} (${mt.name})` : mt.name;
          html += `<span class="detail-tag-chip" style="cursor:pointer;" onclick="closeSemanticModal(); toggleIncludeTag('${escapeJs(mt.name)}')">➕ ${escapeHtml(lb)} <b>(${mt.count})</b></span>`;
        }
        html += `</div>`;
        box.innerHTML = html;
        return;
      }

      const t = d.target_tag;
      let html = `
        <div style="margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border);">
          🎯 目標標籤：<b style="color:#fcd34d; font-size:15px;">${escapeHtml(t.zh_name || t.name)} (${escapeHtml(t.name)})</b>
          — 圖庫中共有 <b>${t.count}</b> 張圖片
          <button class="btn btn-primary" style="padding:2px 8px; font-size:11px; margin-left:8px;" onclick="closeSemanticModal(); toggleIncludeTag('${escapeJs(t.name)}')">篩選此標籤</button>
        </div>
      `;

      if (!d.correlated_all.length) {
        html += `<div style="color:var(--text-muted);">此標籤目前沒有其他共現標籤（請先對該資料夾或圖片執行 AI 自動打標以建立豐富關聯）。</div>`;
        box.innerHTML = html;
        return;
      }

      html += `<table style="width:100%; border-collapse:collapse; text-align:left; font-size:12px;">
        <thead>
          <tr style="border-bottom:1px solid var(--border); color:var(--text-muted);">
            <th style="padding:6px;">關聯標籤 (點擊可直接加入組合篩選)</th>
            <th style="padding:6px;">類別</th>
            <th style="padding:6px;">同時出現張數</th>
            <th style="padding:6px;">條件機率 P(B|A)</th>
            <th style="padding:6px;">相關性分數</th>
          </tr>
        </thead><tbody>`;

      for (const c of d.correlated_all) {
        const lb = c.zh_name ? `${c.zh_name} (${c.name})` : c.name;
        html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
          <td style="padding:6px;">
            <span class="detail-tag-chip" style="cursor:pointer;" onclick="closeSemanticModal(); if(!state.includeTags.includes('${escapeJs(t.name)}')) state.includeTags.push('${escapeJs(t.name)}'); toggleIncludeTag('${escapeJs(c.name)}');" title="點擊同時篩選 [${escapeHtml(t.name)} + ${escapeHtml(c.name)}]">
              🔗 ${escapeHtml(lb)}
            </span>
          </td>
          <td style="padding:6px; color:var(--text-muted);">${escapeHtml(c.category)}</td>
          <td style="padding:6px;"><b>${c.co_count}</b> / ${c.total_count} 張</td>
          <td style="padding:6px; color:#34d399; font-weight:600;">${c.cond_prob_pct}%</td>
          <td style="padding:6px; color:#a5b4fc;">${c.relevance_score}</td>
        </tr>`;
      }
      html += `</tbody></table>`;
      box.innerHTML = html;
    }

    async function runAutoSemanticThemes() {
      const box = document.getElementById('sem-result-box');
      box.innerHTML = '⏳ 正在掃描全圖庫標籤並聚合高階語意主題叢集...';
      const r = await fetch('/api/semantic/apply_themes', { method: 'POST' });
      const d = await r.json();

      let html = `<div style="color:#34d399; font-weight:700; margin-bottom:10px;">
        ✅ 已成功更新 ${d.zh_translations_updated} 個繁體中文標籤翻譯，並建立 ${d.theme_links_added} 筆高階主題關聯！
      </div>`;
      const sum = d.themes_summary || {};
      const keys = Object.keys(sum);
      if (keys.length) {
        html += `<div style="display:flex; flex-wrap:wrap; gap:8px;">`;
        for (const k of keys) {
          html += `<span class="detail-tag-chip" style="cursor:pointer; border-color:#8b5cf6;" onclick="closeSemanticModal(); toggleIncludeTag('${escapeJs(k)}')">✨ ${escapeHtml(k)} <b>(${sum[k]} 張)</b></span>`;
        }
        html += `</div>`;
      } else {
        html += `<div style="color:var(--text-muted);">目前尚未發現符合主題叢集的細部屬性標籤，請先點右上角「🤖 AI 自動打標與畫師辨識」跑幾百張圖後再按一次！</div>`;
      }
      box.innerHTML = html;
      await loadStats();
      await loadTags();
    }

    /* Reverse Image Search & Unified Single-Image Tag+Calibrate Functions */
    async function purgeLowConfAiArtists() {
      if (!confirm('確定要清除所有低於 75% 信心度的本機 AI 畫師猜測，並將未經認證而誤搬入資料夾的圖片還原至根目錄嗎？（原有的資料夾與已認證 Pixiv/Danbooru 畫師不會受影響）')) return;
      const r = await fetch('/api/reverse_search/purge_low_conf_ai?max_ai_conf_to_purge=0.75&revert_unverified_ai_folders=true', { method: 'POST' });
      const d = await r.json();
      alert(`✅ 清理完成！\n• 清除低信心度 AI 畫師猜測：${(d.purged_low_conf_ai_artist_links || 0) + (d.purged_redundant_ai_artist_links || 0)} 筆\n• 還原誤歸檔圖片：${d.reverted_misplaced_ai_images || 0} 張\n• 移除空的誤建資料夾：${d.removed_empty_ai_folders || 0} 個`);
      await loadStats();
      await loadFolders();
      await loadTags();
      await loadImages();
    }

    async function runSingleUnifiedTagAndCalibrate() {
      if (!currentDetailData) return;
      const btn = document.getElementById('btn-single-unified');
      const label = document.getElementById('dt-unified-label');
      const hint = document.getElementById('dt-unified-hint');
      const revBox = document.getElementById('dt-rev-links');
      const oldText = label.textContent;
      const oldHint = hint.textContent;
      const oldHintHidden = hint.hidden;
      if (btn) {
        label.textContent = 'AI 辨識中…';
        hint.textContent = '步驟 1/2 · 正在辨識圖片內容與標籤';
        hint.hidden = false;
        btn.disabled = true;
      }
      if (revBox) revBox.style.display = 'none';

      try {
        // 階段 1：先執行本機 GPU AI 視覺打標
        await fetch(`/api/ai/single/${currentDetailData.id}`, { method: 'POST' });
        await refreshDetailView(currentDetailData.id);

        // 階段 2：接著執行 Danbooru / IQDB / SauceNAO / Google Lens 以圖搜圖校正畫師
        if (btn) {
          label.textContent = '搜圖校正中…';
          hint.textContent = '步驟 2/2 · 正在比對畫師與校正標籤';
        }
        const r = await fetch(`/api/reverse_search/single/${currentDetailData.id}`, { method: 'POST' });
        const d = await r.json();
        if (r.ok) {
          const rev = d.reverse_search_result || {};
          if (rev.found) {
            let msg = `✅ <b>智慧標籤完成！已透過 ${rev.danbooru_hit ? 'Danbooru 100% 結構化認證' : `SauceNAO/IQDB (${rev.similarity}%)`} 校正畫師</b>`;
            if (rev.artists && rev.artists.length) {
              msg += `<br>🎨 認證畫師：<b style="color:#fcd34d;">${escapeHtml(rev.artists.join(', '))}</b>`;
            }
            if (rev.twitter_handles && rev.twitter_handles.length) {
              msg += `<br>🐦 Twitter 帳號：<b>@${rev.twitter_handles.join(', @')}</b>`;
            }
            if (rev.purged_wrong_ai_tags > 0) {
              msg += `<br>🧹 已自動校正並清除 <b>${rev.purged_wrong_ai_tags}</b> 個錯誤的 AI 畫師/角色猜測標籤`;
            }
            let linksArr = [];
            if (rev.google_lens_url) {
              linksArr.push(`<a href="${escapeHtml(rev.google_lens_url)}" target="_blank" style="color:#38bdf8;font-weight:700;text-decoration:underline;margin-right:10px;">🔎 開啟 Google Lens 以圖搜圖比對</a>`);
            }
            if (rev.source_urls && rev.source_urls.length) {
              for (const u of rev.source_urls) {
                linksArr.push(`<a href="${escapeHtml(u)}" target="_blank" style="color:#6ee7b7;text-decoration:underline;margin-right:8px;">🔗 原始來源</a>`);
              }
            }
            if (linksArr.length) {
              msg += `<br>${linksArr.join('')}`;
            }
            revBox.innerHTML = msg;
            revBox.style.display = 'block';
          } else {
            let lensLink = rev.google_lens_url ? `<br><a href="${escapeHtml(rev.google_lens_url)}" target="_blank" style="color:#38bdf8;font-weight:700;text-decoration:underline;">🔎 點此開啟 Google Lens 以圖搜圖網頁比對</a>` : '';
            revBox.innerHTML = `✅ <b>已完成本機 AI 屬性打標！</b><br>⚠️ Danbooru / IQDB / SauceNAO 暫未直接命中來源 ${rev.rate_limited ? '(SauceNAO 達免 Key 頻率限制)' : ''}，可使用 Google Lens 或 Antigravity Gemini MCP 視覺搜圖校正${lensLink}`;
            revBox.style.display = 'block';
          }
          await refreshDetailView(currentDetailData.id);
          await loadStats();
          await loadTags();
          await loadImages();
        }
      } finally {
        if (btn) {
          label.textContent = oldText;
          hint.textContent = oldHint;
          hint.hidden = oldHintHidden;
          btn.disabled = false;
        }
      }
    }

    /* Auto-Ingest & AI Organize Schedule + Interactive PickerTime + Folder Browser */
    let activePickerSlot = 1;
    let currentPickerHour = '03';
    let currentPickerMin = '00';
    let currentBrowsePath = '';
    let currentBrowseParent = '';

    function formatTimeLabel(hm) {
      if (!hm || !hm.includes(':')) return '';
      const [hStr, mStr] = hm.split(':');
      const h = parseInt(hStr, 10);
      const m = parseInt(mStr, 10);
      if (isNaN(h) || isNaN(m)) return hm;
      const hh = String(h).padStart(2, '0');
      const mm = String(m).padStart(2, '0');
      let period = '凌晨';
      if (h === 0) period = '午夜';
      else if (h < 6) period = '凌晨';
      else if (h < 12) period = '上午';
      else if (h === 12) period = '中午';
      else if (h < 18) period = '下午';
      else period = '晚上';
      const h12 = h % 12 === 0 ? 12 : h % 12;
      return `🕒 ${hh}:${mm} (${period} ${String(h12).padStart(2, '0')}:${mm})`;
    }

    function setSlotTime(slotIdx, hm) {
      const inputEl = document.getElementById(`sch-time-${slotIdx}`);
      const btnEl = document.getElementById(`picker-btn-${slotIdx}`);
      const labelEl = document.getElementById(`picker-label-${slotIdx}`);
      const clearBtn = document.getElementById(`picker-clear-${slotIdx}`);
      if (!inputEl || !btnEl || !labelEl) return;

      const cleanHm = (hm || '').trim();
      inputEl.value = cleanHm;

      if (cleanHm) {
        labelEl.textContent = formatTimeLabel(cleanHm);
        btnEl.classList.remove('empty-slot');
        if (clearBtn) clearBtn.style.display = 'inline-flex';
      } else {
        labelEl.textContent = slotIdx === 1 ? '🕒 請選擇時間' : '➕ 點擊選擇第二時段';
        btnEl.classList.add('empty-slot');
        if (clearBtn) clearBtn.style.display = 'none';
      }
    }

    function openTimePicker(slotIdx, ev) {
      if (ev) ev.stopPropagation();
      activePickerSlot = slotIdx;
      const pop = document.getElementById('time-picker-popover');
      const btn = document.getElementById(`picker-btn-${slotIdx}`);
      const existingVal = document.getElementById(`sch-time-${slotIdx}`).value.trim() || (slotIdx === 1 ? '03:00' : '22:00');
      const parts = existingVal.split(':');
      currentPickerHour = String(parseInt(parts[0] || '3', 10)).padStart(2, '0');
      currentPickerMin = String(parseInt(parts[1] || '0', 10)).padStart(2, '0');

      document.getElementById('tp-title').textContent = slotIdx === 1 ? '🕒 選擇第一執行時段' : '🕒 選擇第二執行時段 (選填)';
      document.getElementById('tp-clear-btn').style.display = slotIdx === 2 ? 'inline-flex' : 'none';

      // 若點開時該時段原本是空的，先套用預設值方便直接點確定
      setSlotTime(slotIdx, `${currentPickerHour}:${currentPickerMin}`);
      renderTimePickerColumns();

      pop.classList.add('open');
      if (btn) {
        const rect = btn.getBoundingClientRect();
        let topPos = rect.bottom + 6;
        let leftPos = rect.left;
        if (topPos + 330 > window.innerHeight) {
          topPos = Math.max(12, rect.top - 330);
        }
        if (leftPos + 350 > window.innerWidth) {
          leftPos = Math.max(12, window.innerWidth - 355);
        }
        pop.style.top = `${topPos}px`;
        pop.style.left = `${leftPos}px`;
      }

      setTimeout(() => {
        const activeH = document.querySelector('#tp-hour-list .tp-option.active');
        const activeM = document.querySelector('#tp-min-list .tp-option.active');
        if (activeH) activeH.scrollIntoView({ block: 'center' });
        if (activeM) activeM.scrollIntoView({ block: 'center' });
      }, 20);
    }

    function closeTimePicker() {
      const pop = document.getElementById('time-picker-popover');
      if (pop) pop.classList.remove('open');
    }

    function renderTimePickerColumns() {
      const hm = `${currentPickerHour}:${currentPickerMin}`;
      document.getElementById('tp-preview-badge').textContent = formatTimeLabel(hm).replace('🕒 ', '');

      const hBox = document.getElementById('tp-hour-list');
      let hHtml = '';
      for (let h = 0; h < 24; h++) {
        const hh = String(h).padStart(2, '0');
        let tag = '凌晨';
        if (h === 0) tag = '午夜 12點';
        else if (h < 6) tag = `凌晨 ${h}點`;
        else if (h < 12) tag = `上午 ${h}點`;
        else if (h === 12) tag = '中午 12點';
        else if (h < 18) tag = `下午 ${h - 12}點`;
        else tag = `晚上 ${h - 12}點`;
        const isAct = hh === currentPickerHour ? 'active' : '';
        hHtml += `<div class="tp-option ${isAct}" onclick="selectPickerHour('${hh}')">
          <span><b>${hh}</b> 時</span>
          <span style="font-size:11px; opacity:0.8;">${tag}</span>
        </div>`;
      }
      hBox.innerHTML = hHtml;

      const mBox = document.getElementById('tp-min-list');
      let mHtml = '';
      for (let m = 0; m < 60; m++) {
        const mm = String(m).padStart(2, '0');
        const isAct = mm === currentPickerMin ? 'active' : '';
        const isFive = m % 5 === 0;
        mHtml += `<div class="tp-option ${isAct}" onclick="selectPickerMin('${mm}')">
          <span><b>${mm}</b> 分</span>
          ${isFive ? '<span style="font-size:10px; opacity:0.75;">● 常用</span>' : ''}
        </div>`;
      }
      mBox.innerHTML = mHtml;
    }

    function selectPickerHour(hh) {
      currentPickerHour = hh;
      setSlotTime(activePickerSlot, `${currentPickerHour}:${currentPickerMin}`);
      renderTimePickerColumns();
    }

    function selectPickerMin(mm) {
      currentPickerMin = mm;
      setSlotTime(activePickerSlot, `${currentPickerHour}:${currentPickerMin}`);
      renderTimePickerColumns();
    }

    function applyPresetTime(hm) {
      const [hh, mm] = hm.split(':');
      currentPickerHour = hh;
      currentPickerMin = mm;
      setSlotTime(activePickerSlot, hm);
      renderTimePickerColumns();
      closeTimePicker();
    }

    function clearCurrentPickerSlot() {
      if (activePickerSlot === 2) {
        setSlotTime(2, '');
      }
      closeTimePicker();
    }

    document.addEventListener('click', (e) => {
      const pop = document.getElementById('time-picker-popover');
      if (pop && pop.classList.contains('open')) {
        if (!pop.contains(e.target) && !e.target.closest('.time-picker-wrap')) {
          closeTimePicker();
        }
      }
    });

    function renderSourceFolderChips() {
      const ta = document.getElementById('sch-source-dirs');
      const box = document.getElementById('sch-folder-chips');
      if (!ta || !box) return;
      const dirs = ta.value.split('\n').map(s => s.trim()).filter(Boolean);
      if (!dirs.length) {
        box.innerHTML = '<span class="settings-note">尚未選擇來源資料夾，請點「瀏覽資料夾」加入。</span>';
        return;
      }
      box.innerHTML = dirs.map((d, idx) => `
        <div class="folder-chip-item">
          <span>📁 ${escapeHtml(d)}</span>
          <button class="btn btn-text" aria-label="移除來源資料夾：${escapeHtml(d)}" onclick="removeSourceFolderFromList(${idx})">×</button>
        </div>
      `).join('');
    }

    async function addSourceFolderToList(folderPath) {
      if (!folderPath) return false;
      const clean = folderPath.trim();
      if (!clean) return false;
      const ta = document.getElementById('sch-source-dirs');
      const existing = ta.value.split('\n').map(s => s.trim()).filter(Boolean);
      const key = value => value.replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
      const added = !existing.some(path => key(path) === key(clean));
      if (added) {
        existing.push(clean);
      }
      ta.value = existing.join('\n');
      renderSourceFolderChips();
      await previewSourceDirs();
      return added;
    }

    async function removeSourceFolderFromList(idx) {
      const ta = document.getElementById('sch-source-dirs');
      const existing = ta.value.split('\n').map(s => s.trim()).filter(Boolean);
      existing.splice(idx, 1);
      ta.value = existing.join('\n');
      renderSourceFolderChips();
      if (existing.length) {
        await previewSourceDirs();
      } else {
        document.getElementById('sch-source-preview').style.display = 'none';
      }
    }

    function openFolderBrowserModal() {
      return pickSourceFolderNative();
    }

    function closeFolderBrowserModal() {
      document.getElementById('folder-browser-modal').classList.remove('open');
    }

    async function browseToDirectory(pathStr = '') {
      const subBox = document.getElementById('fb-subdir-list');
      if (subBox) subBox.innerHTML = `<div style="padding:16px; color:var(--text-muted);">⏳ 正在讀取資料夾清單...</div>`;

      const r = await fetch(`/api/schedule/browse_fs?path=${encodeURIComponent(pathStr || '')}`);
      const d = await r.json();

      currentBrowsePath = d.current_path || '';
      currentBrowseParent = d.parent_path || '';

      document.getElementById('fb-path-input').value = currentBrowsePath;
      document.getElementById('fb-selected-display').textContent = currentBrowsePath || '-';
      document.getElementById('fb-up-btn').disabled = !currentBrowseParent;

      // Render Breadcrumbs
      const bcBox = document.getElementById('fb-breadcrumbs');
      const bcs = d.breadcrumbs || [];
      bcBox.innerHTML = bcs.map((b, i) => `
        <span class="fb-breadcrumb-item" onclick="browseToDirectory('${escapeJs(b.path)}')">${escapeHtml(b.name)}</span>
        ${i < bcs.length - 1 ? '<span style="color:#64748b; font-size:11px;">▸</span>' : ''}
      `).join('');

      // Render Quick Access
      const qBox = document.getElementById('fb-quick-list');
      qBox.innerHTML = (d.quick_access || []).map(q => `
        <button class="btn" style="text-align:left; justify-content:flex-start; padding:6px 8px; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; ${q.path === currentBrowsePath ? 'border-color:#38bdf8; color:#38bdf8;' : ''}" onclick="browseToDirectory('${escapeJs(q.path)}')" title="${escapeHtml(q.path)}">
          ${escapeHtml(q.name)}
        </button>
      `).join('');

      // Render Drives
      const drBox = document.getElementById('fb-drive-list');
      drBox.innerHTML = (d.drives || []).map(dr => `
        <button class="btn" style="text-align:left; justify-content:flex-start; padding:6px 8px; font-size:12px; ${currentBrowsePath.startsWith(dr.path) ? 'border-color:#a5b4fc; color:#a5b4fc;' : ''}" onclick="browseToDirectory('${escapeJs(dr.path)}')">
          💽 ${escapeHtml(dr.name)}
        </button>
      `).join('');

      // Render Current Summary & Subdirectories
      const sumBox = document.getElementById('fb-current-summary');
      const subdirs = d.subdirectories || [];
      if (!d.exists) {
        sumBox.innerHTML = `<span style="color:#f87171;">⚠️ 此路徑不存在，請從左側切換磁碟或常用位置</span>`;
        subBox.innerHTML = '';
        return;
      }

      sumBox.innerHTML = `
        <span>📂 目前資料夾內有 <b>${subdirs.length}</b> 個子資料夾、直接包含 <b>${d.direct_image_count || 0}</b> 張圖片/影片</span>
        <span style="font-size:11px; color:var(--text-muted);">點擊資料夾名稱進入下一層，或點右側「➕ 選取」</span>
      `;

      if (!subdirs.length) {
        subBox.innerHTML = `<div style="padding:24px; text-align:center; color:var(--text-muted); font-size:13px;">
          此資料夾底下沒有更深層的子資料夾（直接包含 <b>${d.direct_image_count || 0}</b> 個圖片/影片檔）。<br><br>
          👉 請直接點擊右下角 <b>「✅ 選擇目前所在的資料夾加入排程」</b>！
        </div>`;
        return;
      }

      subBox.innerHTML = subdirs.map(sd => {
        const badges = [];
        if (sd.direct_media_count > 0) {
          badges.push(`<span style="background:rgba(16,185,129,0.16); color:#6ee7b7; padding:1px 6px; border-radius:4px; font-size:11px;">🖼️ ${sd.direct_media_count} 張圖</span>`);
        }
        if (sd.subdir_count > 0) {
          badges.push(`<span style="background:rgba(56,189,248,0.14); color:#7dd3fc; padding:1px 6px; border-radius:4px; font-size:11px;">📁 ${sd.subdir_count} 子夾</span>`);
        }
        if (sd.is_gallery_root) {
          badges.push(`<span style="background:rgba(245,158,11,0.2); color:#fcd34d; padding:1px 6px; border-radius:4px; font-size:11px;">⚠️ 圖庫本體</span>`);
        }
        return `
          <div class="fb-dir-row" onclick="browseToDirectory('${escapeJs(sd.path)}')">
            <div style="display:flex; align-items:center; gap:8px; overflow:hidden;">
              <span style="font-size:15px;">📁</span>
              <span style="font-size:13px; font-weight:600; color:#f1f5f9; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(sd.name)}</span>
              ${badges.join('')}
            </div>
            <div style="display:flex; gap:6px; align-items:center;" onclick="event.stopPropagation()">
              <button class="btn" style="padding:3px 8px; font-size:11px;" onclick="browseToDirectory('${escapeJs(sd.path)}')">進入 ▸</button>
              ${sd.is_gallery_root ? '' : `<button class="btn btn-primary" style="padding:3px 10px; font-size:11px; background:#059669; border-color:#10b981;" onclick="selectSpecificFolderFromBrowser('${escapeJs(sd.path)}')">➕ 選取此夾</button>`}
            </div>
          </div>
        `;
      }).join('');
    }

    function browseUpDirectory() {
      if (currentBrowseParent) {
        browseToDirectory(currentBrowseParent);
      }
    }

    async function selectSpecificFolderFromBrowser(pathStr) {
      await addSourceFolderToList(pathStr);
      closeFolderBrowserModal();
    }

    async function selectCurrentBrowsedFolder() {
      if (!currentBrowsePath) return;
      await addSourceFolderToList(currentBrowsePath);
      closeFolderBrowserModal();
    }

    let nativeFolderPickerPending = false;
    async function pickSourceFolderNative(initDir = '') {
      if (nativeFolderPickerPending) return;
      const button = document.getElementById('btn-pick-source-folder');
      const status = document.getElementById('sch-folder-picker-status');
      const sources = document.getElementById('sch-source-dirs').value.split('\n').map(s => s.trim()).filter(Boolean);
      const initialDir = initDir || sources[sources.length - 1] || '';
      nativeFolderPickerPending = true;
      button.disabled = true;
      button.textContent = '選擇中…';
      status.textContent = '請在 Windows 資料夾選擇視窗中操作。';
      closeFolderBrowserModal();
      try {
        const r = await fetch(`/api/schedule/pick_folder?initial_dir=${encodeURIComponent(initialDir)}`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '無法開啟資料夾選擇視窗');
        if (typeof d.selected_path !== 'string') throw new Error('資料夾選取回應異常');
        if (!d.selected_path.trim()) {
          status.textContent = '已取消選擇。';
          return;
        }
        status.textContent = '已選擇資料夾，正在檢查來源…';
        const added = await addSourceFolderToList(d.selected_path);
        status.textContent = added ? '已加入來源資料夾。儲存排程後生效。' : '這個資料夾已在來源清單中。';
      } catch (error) {
        status.textContent = `${error.message}。請重試，或展開「手動貼上路徑」。`;
      } finally {
        nativeFolderPickerPending = false;
        button.disabled = false;
        button.textContent = '瀏覽資料夾…';
        if (document.getElementById('schedule-modal').classList.contains('open')) button.focus();
      }
    }

    let settingsReturnFocus = null;
    let scheduleConfigSnapshot = {};
    let scheduleConfigLoaded = false;
    let scheduleLoadSequence = 0;
    let currentOpLogCat = 'all';
    let opLogTimer = null;

    function openSettings(section = 'browse') {
      settingsReturnFocus = document.activeElement;
      hideHoverPreview();
      syncBrowsePreferences();
      document.getElementById('schedule-modal').classList.add('open');
      switchSettingsTab(section);
      document.getElementById(`settings-tab-${section}`).focus();
      loadScheduleConfig();
      loadOperationLogs();
    }

    function openScheduleModal() { openSettings('schedule'); }

    function switchSettingsTab(section) {
      const base = function (section) {
      closeTimePicker();
      for (const name of ['browse', 'schedule', 'history']) {
        const selected = name === section;
        const tab = document.getElementById(`settings-tab-${name}`);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        document.getElementById(`settings-${name}`).hidden = !selected;
      }
      document.getElementById('schedule-footer-actions').hidden = section !== 'schedule';
      document.getElementById('btn-close-settings').hidden = section === 'schedule';
      document.getElementById('settings-footer-note').textContent = section === 'schedule'
        ? '修改後請儲存；立即執行會搬移檔案。'
        : section === 'history' ? '記錄除搜尋與過濾外的所有功能處理（同步寫入 SQLite 與 activity.log）' : '瀏覽偏好套用後自動儲存';
      document.querySelector('.settings-content').scrollTop = 0;
      if (section === 'history') {
        loadOperationLogs();
      }
      };
      return window.GalleryApp ? window.GalleryApp.run("switchSettingsTab", Array.from(arguments), base) : base(section);
    }

    function setOpLogCategory(cat, btnEl) {
      currentOpLogCat = cat || 'all';
      document.querySelectorAll('#op-log-cat-pills [data-log-cat]').forEach(el => {
        el.classList.toggle('active', el.dataset.logCat === currentOpLogCat);
      });
      loadOperationLogs();
    }

    function debounceLoadOperationLogs() {
      clearTimeout(opLogTimer);
      opLogTimer = setTimeout(loadOperationLogs, 220);
    }

    const OP_CAT_LABELS = {
      ai: { label: '🤖 AI 打標', color: '#a5b4fc', bg: 'rgba(99,102,241,0.18)' },
      search_calibrate: { label: '🔍 搜圖/Pixiv', color: '#6ee7b7', bg: 'rgba(16,185,129,0.18)' },
      organize: { label: '📂 資料夾歸檔', color: '#fcd34d', bg: 'rgba(245,158,11,0.18)' },
      rename: { label: '✏️ 批次改名', color: '#fdba74', bg: 'rgba(249,115,22,0.18)' },
      tag_edit: { label: '🏷️ 標籤變更', color: '#7dd3fc', bg: 'rgba(56,189,248,0.18)' },
      decensor: { label: '✨ 去馬賽克', color: '#f9a8d4', bg: 'rgba(236,72,153,0.18)' },
      semantic: { label: '🧠 語意主題', color: '#c4b5fd', bg: 'rgba(139,92,246,0.18)' },
      scan: { label: '🔄 圖庫掃描', color: '#93c5fd', bg: 'rgba(59,130,246,0.18)' },
      schedule: { label: '🕒 定時排程', color: '#5eead4', bg: 'rgba(20,184,166,0.18)' },
    };

    async function loadOperationLogs() {
      const box = document.getElementById('op-log-list');
      if (!box) return;
      const q = (document.getElementById('op-log-search')?.value || '').trim();
      try {
        const params = new URLSearchParams({ category: currentOpLogCat, q, limit: '150' });
        const r = await fetch(`/api/logs?${params.toString()}`);
        if (!r.ok) throw new Error('讀取失敗');
        const d = await r.json();
        const totalBadge = document.getElementById('op-log-total-badge');
        if (totalBadge) totalBadge.textContent = String(d.total ?? 0);

        const logs = d.logs || d.items || [];
        if (!logs.length) {
          box.innerHTML = `<div style="padding:18px; text-align:center; color:var(--text-muted); border:1px dashed var(--border); border-radius:8px;">尚無符合條件的操作日誌紀錄。</div>`;
          return;
        }

        box.innerHTML = logs.map(lg => {
          const catMeta = OP_CAT_LABELS[lg.category] || { label: lg.category, color: '#cbd5e1', bg: 'rgba(148,163,184,0.15)' };
          const statusColor = lg.status === 'error' ? '#f87171' : (lg.status === 'warning' ? '#fbbf24' : '#34d399');
          const statusIcon = lg.status === 'error' ? '❌' : (lg.status === 'warning' ? '⚠️' : '✅');
          const durTxt = lg.duration_sec > 0 ? `<span style="color:#94a3b8; font-size:11px;">⏱️ ${lg.duration_sec}s</span>` : '';
          const cntTxt = lg.affected_count > 0 ? `<span style="color:#38bdf8; font-size:11px; font-weight:600;">📊 影響 ${Number(lg.affected_count).toLocaleString()} 筆</span>` : '';
          const hasDetails = lg.details && Object.keys(lg.details).length > 0;
          const detailsHtml = hasDetails
            ? `<details style="margin-top:4px;"><summary style="cursor:pointer; font-size:11px; color:#94a3b8;">檢視詳細參數與統計數據 (JSON)</summary><pre style="margin:4px 0 0; padding:8px; background:rgba(0,0,0,0.35); border-radius:6px; font-size:11px; color:#cbd5e1; overflow-x:auto; white-space:pre-wrap;">${escapeHtml(JSON.stringify(lg.details, null, 2))}</pre></details>`
            : '';
          return `
            <div style="padding:9px 11px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.07); border-left:3px solid ${statusColor}; border-radius:7px; display:flex; flex-direction:column; gap:4px;">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                  <span style="font-family:Consolas,monospace; font-size:11px; color:#fcd34d;">[${escapeHtml(lg.timestamp || lg.created_at || '')}]</span>
                  <span style="padding:1px 7px; border-radius:999px; font-size:11px; font-weight:600; color:${catMeta.color}; background:${catMeta.bg};">${escapeHtml(catMeta.label)}</span>
                  <b style="font-size:13px; color:#f8fafc;">${statusIcon} ${escapeHtml(lg.action)}</b>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                  ${cntTxt}
                  ${durTxt}
                </div>
              </div>
              <div style="font-size:12px; color:#cbd5e1; line-height:1.45;">${escapeHtml(lg.summary)}</div>
              ${detailsHtml}
            </div>
          `;
        }).join('');
      } catch (err) {
        box.innerHTML = `<div style="padding:12px; color:#f87171;">無法讀取操作日誌：${escapeHtml(err.message)}</div>`;
      }
    }

    async function openActivityLogFile() {
      try {
        await fetch('/api/logs/open_file', { method: 'POST' });
      } catch (_) {}
    }

    async function clearOperationLogs() {
      if (!confirm('確定要清空所有歷史操作日誌紀錄嗎？')) return;
      try {
        await fetch('/api/logs/clear', { method: 'POST' });
        await loadOperationLogs();
      } catch (_) {}
    }

    function handleSettingsTabKeydown(e) {
      const base = function (e) {
      const names = ['browse', 'schedule', 'history'];
      const index = names.findIndex(name => e.target.id === `settings-tab-${name}`);
      if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      e.preventDefault();
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? 2 : (index + (e.key === 'ArrowLeft' ? 2 : 1)) % 3;
      switchSettingsTab(names[next]);
      document.getElementById(`settings-tab-${names[next]}`).focus();
      };
      return window.GalleryApp ? window.GalleryApp.run("handleSettingsTabKeydown", Array.from(arguments), base) : base(e);
    }

    function setScheduleFeedback(message, isError = false) {
      const feedback = document.getElementById('schedule-feedback');
      feedback.textContent = message;
      feedback.classList.toggle('error', isError);
    }

    function closeScheduleModal() {
      const base = function () {
      closeTimePicker();
      document.getElementById('schedule-modal').classList.remove('open');
      settingsReturnFocus?.focus();
      };
      return window.GalleryApp ? window.GalleryApp.run("closeScheduleModal", Array.from(arguments), base) : base();
    }

    async function loadScheduleConfig() {
      const sequence = ++scheduleLoadSequence;
      scheduleConfigLoaded = false;
      document.getElementById('btn-save-schedule').disabled = true;
      document.getElementById('btn-sch-run-now').disabled = true;
      setScheduleFeedback('正在讀取排程設定…');
      try {
        const r = await fetch('/api/schedule/status');
        if (!r.ok) throw new Error('無法讀取排程設定');
        const d = await r.json();
        if (sequence !== scheduleLoadSequence) return;
        const cfg = d.config || {};
        scheduleConfigSnapshot = cfg;

        document.getElementById('sch-enabled').checked = !!cfg.enabled;
        const times = cfg.schedule_times || ['03:00'];
        setSlotTime(1, times[0] || '03:00');
        setSlotTime(2, times[1] || '');
        document.getElementById('sch-source-dirs').value = (cfg.source_dirs || []).join('\n');
        renderSourceFolderChips();
        document.getElementById('sch-rm-empty').checked = cfg.remove_empty_subdirs !== false;
        document.getElementById('sch-run-pixiv').checked = cfg.run_pixiv_lookup !== false;
        document.getElementById('sch-run-ai').checked = cfg.run_ai_tagger !== false;
        document.getElementById('sch-run-rev').checked = !!cfg.run_reverse_search;
        document.getElementById('sch-auto-folder').checked = cfg.auto_create_artist_folders !== false;
        document.getElementById('sch-auto-rename').checked = cfg.auto_rename_in_artist_folder !== false;
        document.getElementById('sch-also-root').checked = !!cfg.also_organize_all_root;
        renderScheduleHistory(d);
        scheduleConfigLoaded = true;
        setScheduleFeedback('');
      } catch (error) {
        if (sequence === scheduleLoadSequence) {
          setScheduleFeedback('讀取失敗，請關閉設定後重試。', true);
          document.getElementById('sch-history-box').textContent = '無法讀取紀錄，請重新開啟設定。';
        }
      } finally {
        if (sequence === scheduleLoadSequence) {
          document.getElementById('btn-save-schedule').disabled = !scheduleConfigLoaded;
          document.getElementById('btn-sch-run-now').disabled = !scheduleConfigLoaded;
        }
      }
    }

    function renderScheduleHistory(d) {
      const cfg = d.config || {};
      document.getElementById('sch-live-status').textContent = d.status_message || '待機中';

      const histBox = document.getElementById('sch-history-box');
      const logs = cfg.history_logs || [];
      if (!logs.length) {
        histBox.textContent = '尚無執行紀錄。完成匯入排程設定後，這裡會顯示執行結果。';
      } else {
        histBox.innerHTML = logs.map(lg => `
          <div style="padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
            <span style="color:#fcd34d;">[${escapeHtml(lg.timestamp)}]</span>
            <b>${escapeHtml(lg.trigger_reason)}</b> —
            搬入: <b style="color:#38bdf8;">${lg.moved_from_source}</b> 張 |
            清空空夾: <b style="color:#f472b6;">${lg.empty_dirs_removed}</b> 個 |
            AI打標: <b style="color:#a5b4fc;">${lg.ai_tagged_count}</b> 張 |
            歸檔繪師夾: <b style="color:#34d399;">${lg.organized_to_artist_folders}</b> 張 (新建 ${lg.new_artist_folders_created} 夾)
            <span style="color:#64748b;">(${lg.elapsed_sec}s)</span>
          </div>
        `).join('');
      }
    }

    async function previewSourceDirs() {
      const dirs = document.getElementById('sch-source-dirs').value
        .split('\n')
        .map(s => s.trim())
        .filter(Boolean);
      const box = document.getElementById('sch-source-preview');
      if (!dirs.length) {
        box.style.display = 'block';
        box.innerHTML = '⚠️ 請先輸入或選擇至少一個來源資料夾路徑';
        return;
      }
      box.style.display = 'block';
      box.innerHTML = '⏳ 正在掃描來源資料夾與子資料夾...';

      try {
        const r = await fetch('/api/schedule/preview_source', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source_dirs: dirs })
        });
        if (!r.ok) throw new Error('掃描失敗');
        const d = await r.json();
        let html = `📊 掃描結果：共發現 <b>${d.total_files}</b> 個圖片/影片檔案，分布在 <b>${d.total_subdirs}</b> 個子資料夾中：<br>`;
        for (const item of (d.directories || [])) {
          if (!item.exists) {
            html += `• ❌ <code>${escapeHtml(item.path)}</code> (路徑不存在)<br>`;
          } else {
            html += `• ✅ <code>${escapeHtml(item.path)}</code>：<b>${item.file_count}</b> 個檔案 / <b>${item.subdir_count}</b> 個子資料夾<br>`;
          }
        }
        box.innerHTML = html;
      } catch (_) {
        box.textContent = '無法檢查來源資料夾，請確認路徑後重試。';
      }
    }

    function collectSchedulePayload() {
      const t1 = document.getElementById('sch-time-1').value.trim();
      const t2 = document.getElementById('sch-time-2').value.trim();
      const times = [t1, t2].filter(Boolean);
      const dirs = document.getElementById('sch-source-dirs').value
        .split('\n')
        .map(s => s.trim())
        .filter(Boolean);

      return {
        reverse_search_limit: scheduleConfigSnapshot.reverse_search_limit ?? 30,
        general_thresh: scheduleConfigSnapshot.general_thresh ?? 0.70,
        artist_thresh: scheduleConfigSnapshot.artist_thresh ?? 0.70,
        min_artist_confidence: scheduleConfigSnapshot.min_artist_confidence ?? 0.70,
        enabled: document.getElementById('sch-enabled').checked,
        schedule_times: times.length ? times : ['03:00'],
        source_dirs: dirs,
        remove_empty_subdirs: document.getElementById('sch-rm-empty').checked,
        run_pixiv_lookup: document.getElementById('sch-run-pixiv').checked,
        run_ai_tagger: document.getElementById('sch-run-ai').checked,
        run_reverse_search: document.getElementById('sch-run-rev').checked,
        auto_create_artist_folders: document.getElementById('sch-auto-folder').checked,
        auto_rename_in_artist_folder: document.getElementById('sch-auto-rename').checked,
        also_organize_all_root: document.getElementById('sch-also-root').checked,
        sync_windows_task: document.getElementById('sch-win-task').checked
      };
    }

    async function saveScheduleConfig(silent = false) {
      if (!scheduleConfigLoaded) return false;
      const payload = collectSchedulePayload();
      if (payload.enabled && !payload.source_dirs.length) {
        setScheduleFeedback('請先選擇至少一個來源資料夾。', true);
        return false;
      }
      document.getElementById('btn-save-schedule').disabled = true;
      document.getElementById('btn-sch-run-now').disabled = true;
      setScheduleFeedback('正在儲存…');
      try {
        const r = await fetch('/api/schedule/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!r.ok) throw new Error(`伺服器回應 ${r.status}`);
        const d = await r.json();
        scheduleConfigSnapshot = d.config || payload;
        let message = payload.enabled ? `已儲存 · 每日 ${payload.schedule_times.join('、')} 執行` : '已儲存 · 每日排程已停用';
        if (d.windows_task) {
          message += d.windows_task.registered ? ' · 已同步 Windows 排程' : payload.enabled ? ' · Windows 排程同步未成功，請檢查系統工作排程器' : ' · 已移除 Windows 排程';
        }
        setScheduleFeedback(silent ? '設定已儲存，正在啟動…' : message);
        return true;
      } catch (error) {
        setScheduleFeedback(`儲存失敗：${error.message}。請重試。`, true);
        return false;
      } finally {
        document.getElementById('btn-save-schedule').disabled = false;
        document.getElementById('btn-sch-run-now').disabled = false;
      }
    }

    async function runScheduleNow() {
      if (!await saveScheduleConfig(true)) return;
      document.getElementById('btn-sch-run-now').disabled = true;
      try {
        const r = await fetch('/api/schedule/run_now', { method: 'POST' });
        if (!r.ok) throw new Error(`伺服器回應 ${r.status}`);
        const d = await r.json();
        setScheduleFeedback(d.status === 'already_running' ? d.message || '排程正在執行中' : '已開始執行，可到「執行紀錄」查看進度。');
        checkAiStatus();
      } catch (error) {
        setScheduleFeedback(`設定已儲存，但啟動失敗：${error.message}`, true);
      } finally {
        document.getElementById('btn-sch-run-now').disabled = false;
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/[&<>"']/g, s => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[s]));
    }
    function escapeJs(str) {
      if (!str) return '';
      return escapeHtml(String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r/g, '\\r').replace(/\n/g, '\\n'));
    }
