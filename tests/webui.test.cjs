// Run with: node --test tests/webui.test.cjs
// Exercise the shipped script with isolated browser stubs; never call the live scheduler.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../static/gallery-app.js'), 'utf8');

test('duplicate preview labels exact pixels and selects byte copies within a mixed group', () => {
  const f = fixture();
  f.run(`currentDedupPreview = {
    total_scanned: 3, total_groups: 1, exact_duplicate_images: 1, visual_duplicate_images: 1,
    total_duplicate_images: 2, all_delete_ids: [2, 3], groups: [{
      group_id: 1, match_type: 'visual', folder: 'artist', reclaim_bytes: 300,
      keep: {id: 1, filename: 'master.png', folder: 'artist', width: 64, height: 64},
      duplicates: [
        {id: 2, filename: 'copy.png', folder: 'artist', size_bytes: 100, metrics: {method: 'exact_bytes'}},
        {id: 3, filename: 'lossless.webp', folder: 'artist', size_bytes: 200, metrics: {method: 'opencv_exact_pixels'}}
      ]
    }]
  }; selectedDedupIds = new Set([2, 3]); renderDedupPreview();`);
  assert.match(f.element('dedup-preview-box').innerHTML, /原尺寸像素完全相同/);
  assert.doesNotMatch(f.element('dedup-summary-bar').innerHTML, /視覺相似/);
  f.run("selectDedupFilter('exact')");
  assert.equal(f.run('[...selectedDedupIds].join()'), '2');
  assert.match(f.element('btn-dedup-exec').textContent, /1 張/);
});

test('rescanning clears the previous duplicate deletion selection even on failure', async () => {
  const f = fixture();
  f.run('currentDedupPreview = {groups: []}; selectedDedupIds = new Set([99]);');
  f.context.fetch = async () => ({ok: false, json: async () => ({detail: 'scan failed'})});
  await f.run('loadDedupPreview()');
  assert.equal(f.run('currentDedupPreview'), null);
  assert.equal(f.run('selectedDedupIds.size'), 0);
  assert.equal(f.element('btn-dedup-exec').disabled, true);
});

test('duplicate scans poll progress and closing stops polling without accepting a late result', async () => {
  const f = fixture();
  let requests = 0;
  f.context.fetch = async () => {
    requests++;
    return {ok: true, json: async () => ({scan_id: 'shared', status: 'running', elapsed_seconds: 2,
      progress: {stage: '建立候選圖像素索引', processed_count: 25, total_target: 100, cached_images: 20}})};
  };
  const pending = f.run('loadDedupPreview()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.element('dedup-progress').hidden, false);
  assert.match(f.element('dedup-progress-stage').textContent, /建立候選/);
  assert.equal(f.element('dedup-progress-bar').value, 25);
  f.run('closeDedupModal()');
  f.tick();
  await pending;
  assert.equal(requests, 1);
  assert.equal(f.run('currentDedupPreview'), null);
});

test('a completed duplicate scan replaces progress with verified results', async () => {
  const f = fixture();
  let requestCount = 0;
  f.context.fetch = async () => ({ok: true, json: async () => ++requestCount === 1
    ? {scan_id: 'scan-1', status: 'running', progress: {stage: '檢查尺寸與快取', processed_count: 1, total_target: 2}}
    : {scan_id: 'scan-1', status: 'completed', result: {total_scanned: 2, total_groups: 0, groups: [], all_delete_ids: []}}});
  const pending = f.run('loadDedupPreview()');
  await new Promise(resolve => setImmediate(resolve));
  f.tick();
  await pending;
  assert.equal(f.element('dedup-progress').hidden, true);
  assert.equal(f.element('dedup-preview-box').style.display, 'flex');
  assert.equal(f.run('currentDedupPreview.total_scanned'), 2);
  assert.equal(f.element('btn-dedup-exec').disabled, true);
});

test('tag selection updates the cached sidebar without another tag request', async () => {
  const f = fixture();
  let requests = 0;
  f.context.fetch = async () => {
    requests++;
    return { ok: true, json: async () => ({items:[{name:'artist-a', zh_name:'畫師甲', category:'artist', count:10}], next_offset:1, has_more:false}) };
  };
  f.run('loadImages = () => {}');
  await f.run('loadTags()');
  f.run("toggleIncludeTag('artist-a')");
  assert.equal(requests, 1);
  assert.match(f.element('sidebar-tag-list').innerHTML, /aria-pressed="true"/);
  f.run('clearTagFilters()');
  assert.equal(requests, 1);
  assert.match(f.element('sidebar-tag-list').innerHTML, /aria-pressed="false"/);
});

test('late tag-category responses cannot overwrite the current sidebar', async () => {
  const f = fixture();
  const pending = [];
  f.context.fetch = () => new Promise(resolve => pending.push(resolve));
  const old = f.run('loadTags()');
  const current = f.run("state.tagCategory = 'artist'; loadTags()");
  pending[1]({ok:true, json: async () => ({items:[{name:'current', category:'artist', count:10}], next_offset:1, has_more:false})});
  await current;
  pending[0]({ok:true, json: async () => ({items:[{name:'outdated', category:'general', count:20}], next_offset:1, has_more:false})});
  await old;
  assert.match(f.element('sidebar-tag-list').innerHTML, /current/);
  assert.doesNotMatch(f.element('sidebar-tag-list').innerHTML, /outdated/);
});

test('artist rows show one name and alias clicks share one removable filter', () => {
  const f = fixture();
  f.run('loadImages = () => {}; state.includeTags = ["hara_(harayutaka)", "hara"];');
  f.run(`renderTagList([
    {name:'hara', zh_name:'現有繪師：Hara', category:'artist', count:4, aliases:['hara','hara_(harayutaka)']},
    {name:'blue', zh_name:'藍色', category:'general', count:2}
  ])`);
  const sidebar = f.element('sidebar-tag-list').innerHTML;
  assert.doesNotMatch(sidebar, /現有繪師/);
  assert.equal((sidebar.match(/class="tag-label-en"/g) || []).length, 1);
  assert.match(sidebar, /class="tag-label-zh">Hara<\/span>/);
  assert.equal(f.run('state.includeTags.join(",")'), 'hara');
  assert.match(f.element('active-filters-bar').innerHTML, />Hara /);
  f.run("toggleIncludeTag('hara_(harayutaka)')");
  assert.equal(f.run('state.includeTags.length'), 0);
  f.run("toggleIncludeTag('hara_(harayutaka)')");
  assert.equal(f.run('state.includeTags.join(",")'), 'hara');
  f.run('clearTagFilters()');
  assert.doesNotMatch(f.element('sidebar-tag-list').innerHTML, /aria-pressed="true"/);
});

test('sidebar tags load a bounded first batch and only one next page at a time', async () => {
  const f = fixture(), requests = [], pending = [];
  f.context.fetch = (url, options) => {
    requests.push({url:new URL(url, 'http://gallery.test'), options});
    return new Promise(resolve => pending.push(resolve));
  };
  const initial = f.run('loadTags()');
  assert.equal(requests[0].url.searchParams.get('limit'), '40');
  assert.equal(requests[0].url.searchParams.get('offset'), '0');
  assert.equal(requests[0].url.searchParams.get('paged'), 'true');
  pending[0]({ok:true,json:async()=>({items:[{name:'first',category:'general',count:5}],next_offset:1,has_more:true})});
  await initial;
  assert.equal(requests.length, 1, 'No eager request for later pages');
  const next = f.run('loadMoreTags()');
  await f.run('loadMoreTags()');
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url.searchParams.get('offset'), '1');
  pending[1]({ok:true,json:async()=>({items:[{name:'second',category:'general',count:4}],next_offset:2,has_more:false})});
  await next;
  assert.equal(f.run("sidebarTags.map(t=>t.name).join(',')"), 'first,second');
  await f.run('loadMoreTags()');
  assert.equal(requests.length, 2, 'Stop at the end of the list');
});

test('a failed later tag page keeps loaded tags and retries the same offset', async () => {
  const f = fixture(), offsets = [];
  let failed = false;
  f.context.fetch = async url => {
    const offset = Number(new URL(url, 'http://gallery.test').searchParams.get('offset'));
    offsets.push(offset);
    if (offset === 1 && !failed) { failed = true; return {ok:false}; }
    return {ok:true,json:async()=>({items:[{name:offset ? 'second' : 'first',category:'general',count:1}], next_offset:offset+1,has_more:offset===0})};
  };
  await f.run('loadTags()');
  assert.equal(await f.run('loadMoreTags()'), false);
  assert.equal(f.run('sidebarTags[0].name'), 'first');
  assert.match(f.element('tag-list-status').innerHTML, /重試/);
  await f.run('loadMoreTags()');
  assert.deepEqual(offsets, [0,1,1]);
  assert.equal(f.run('sidebarTags.length'), 2);
});

test('a stale appended tag page cannot join a new search or clear its loading state', async () => {
  const f = fixture(), pending = [];
  f.context.fetch = () => new Promise(resolve => pending.push(resolve));
  const initial = f.run('loadTags()');
  pending[0]({ok:true,json:async()=>({items:[{name:'old-first',category:'general'}],next_offset:1,has_more:true})});
  await initial;
  const old = f.run('loadMoreTags()');
  f.element('tag-search-input').value = 'new';
  const current = f.run('loadTags()');
  pending[1]({ok:true,json:async()=>({items:[{name:'old-next',category:'general'}],next_offset:2,has_more:false})});
  await old;
  assert.equal(f.run('tagPaging.loading'), true);
  pending[2]({ok:true,json:async()=>({items:[{name:'new-match',category:'general'}],next_offset:1,has_more:false})});
  await current;
  assert.equal(f.run("sidebarTags.map(t=>t.name).join(',')"), 'new-match');
  assert.equal(f.run('tagPaging.loading'), false);
});

function fixture(saved = '{}') {
  const elements = new Map(), timers = new Map(), images = [];
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        value: '', checked: false, hidden: true, disabled: false, textContent: '', innerHTML: '', style: {}, dataset: {},
        classList: { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
          toggle(c, value) { if (value) classes.add(c); else classes.delete(c); } },
        setAttribute() {}, removeAttribute() {}, reportValidity: () => true,
        focus() {}, getClientRects: () => [1],
        insertAdjacentHTML(_position, markup) { element('sidebar-tag-list').innerHTML += markup; }
      });
    }
    return elements.get(id);
  }
  const document = {
    getElementById: element, addEventListener() {}, querySelectorAll: () => [],
    querySelector(selector) {
      if (selector.includes('.modal-backdrop.open')) {
        const ids = ['schedule-modal', 'folder-browser-modal', 'ai-modal', 'detail-modal'];
        return ids.filter(id => !(selector.includes(':not(#detail-modal)') && id === 'detail-modal'))
          .map(element).find(el => el.classList.contains('open')) || null;
      }
      return null;
    }
  };
  const context = vm.createContext({
    document, window: { addEventListener() {}, innerWidth: 1200, innerHeight: 800 },
    localStorage: { getItem: () => saved, setItem: (_key, value) => { saved = value; } },
    URLSearchParams, AbortController, console,
    fetch: () => { throw Error('Unexpected network request'); },
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    Image: class { constructor() { images.push(this); } },
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  return { run, context, element, images, stored: () => JSON.parse(saved),
    tick() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); } };
}

test('folder action reveals the current image and reports missing files or network failures', async () => {
  const f = fixture();
  const requests = [], alerts = [];
  f.context.alert = message => alerts.push(message);
  f.context.fetch = async (url, options) => {
    requests.push([url, options.method]);
    return { ok: true };
  };
  await f.run('openInExplorer()');
  assert.equal(requests.length, 0);
  f.run('currentDetailData = { id: 42 };');
  await f.run('openInExplorer()');
  assert.deepEqual(requests, [['/api/open_explorer/42', 'POST']]);
  assert.deepEqual(alerts, []);
  f.context.fetch = async () => ({ ok: false, json: async () => ({ detail: '圖片檔案不存在' }) });
  await f.run('openInExplorer()');
  assert.match(alerts.pop(), /圖片檔案不存在/);
  f.context.fetch = async () => { throw Error('連線中斷'); };
  await f.run('openInExplorer()');
  assert.match(alerts.pop(), /連線中斷/);
});

function key(f, overrides = {}) {
  const event = { key: 'ArrowRight', target: { closest: () => null }, preventDefault() { this.prevented = true; }, ...overrides };
  f.context.event = event;
  f.run('handleBrowseKeydown(event)');
  return event;
}

test('preferences reject malformed/out-of-range values and retain valid custom counts', () => {
  assert.equal(fixture('broken json').run('state.pageSize'), 60);
  assert.equal(fixture('{"pageSize":201}').run('state.pageSize'), 60);
  assert.equal(fixture('{"pageSize":10.5}').run('state.pageSize'), 60);
  const f = fixture('{"pageSize":37,"hoverPreview":false}');
  assert.equal(f.run('state.pageSize'), 37);
  assert.equal(f.run('browsePreferences.hoverPreview'), false);
});

test('page sizes reset pagination, synchronize both controls, and tolerate unavailable storage', () => {
  const f = fixture();
  f.run('loadImages = () => {}; state.page = 5;');
  f.element('page-size-input').value = '37';
  f.run("applyPageSize('page-size-input')");
  assert.equal(f.run('state.page'), 1);
  assert.equal(f.run('state.pageSize'), 37);
  assert.equal(f.element('preference-page-size').value, 37);
  assert.equal(f.stored().pageSize, 37);
  f.element('page-size-input').value = '201';
  f.run("applyPageSize('page-size-input')");
  assert.equal(f.run('state.pageSize'), 37);
  f.run("localStorage.setItem = () => { throw Error('blocked'); }; updateBrowsePreference('hoverPreview', false)");
  assert.equal(f.run('browsePreferences.hoverPreview'), false);
  assert.match(f.element('preferences-status').textContent, /無法儲存/);
});

test('keyboard paging respects editable controls, composition, modifiers, repetition and dialogs', () => {
  const f = fixture();
  f.run('let pages = 0, details = 0; changePage = delta => { pages += delta; }; navigateDetail = delta => { details += delta; };');
  for (const overrides of [
    { target: { closest: () => ({}) } }, { isComposing: true }, { ctrlKey: true },
    { altKey: true }, { repeat: true }, { defaultPrevented: true }
  ]) key(f, overrides);
  for (const id of ['schedule-modal', 'ai-modal', 'folder-browser-modal']) {
    f.element(id).classList.add('open'); key(f); f.element(id).classList.remove('open');
  }
  assert.equal(f.run('pages'), 0);
  key(f);
  assert.equal(f.run('pages'), 1);
  f.run('browsePreferences.keyboardPaging = false');
  key(f);
  assert.equal(f.run('pages'), 1);
  f.element('detail-modal').classList.add('open');
  key(f, { key: 'ArrowLeft' });
  assert.equal(f.run('details'), -1);
  assert.equal(f.run('pages'), 1);
});

test('page navigation cannot pass the first or final page', () => {
  const f = fixture();
  f.run('loadImages = () => {}; state.total = 61;');
  f.run('changePage(-1)'); assert.equal(f.run('state.page'), 1);
  f.run('changePage(1); changePage(1)'); assert.equal(f.run('state.page'), 2);
  f.run('changePage(-1)'); assert.equal(f.run('state.page'), 1);
});

test('tags toggle on and off; clearing tags preserves folder and search', () => {
  const f = fixture();
  f.run('loadImages = () => {}; loadTags = () => {};');
  f.run("toggleIncludeTag('a'); toggleIncludeTag('b'); toggleIncludeTag('a');");
  assert.equal(f.run("state.includeTags.join(',')"), '最近,b');
  f.run("toggleIncludeTag('a')");
  assert.equal(f.run("state.includeTags.join(',')"), '最近,b,a');
  f.element('main-search').value = 'kept search';
  f.run("state.folder = 'kept folder'; clearTagFilters()");
  assert.equal(f.run('state.folder'), 'kept folder');
  assert.equal(f.element('main-search').value, 'kept search');
  assert.equal(f.run('state.includeTags.length'), 0);
});

test('out-of-order image requests cannot replace newer results', async () => {
  const f = fixture();
  const pending = [];
  f.context.fetch = () => new Promise(resolve => pending.push(resolve));
  f.run('renderGrid = () => {}; renderPagination = () => {};');
  const first = f.run('loadImages()');
  const second = f.run('state.page = 2; loadImages()');
  pending[1]({ ok: true, json: async () => ({ items: [{ id: 2 }], total: 120 }) });
  await second;
  pending[0]({ ok: true, json: async () => ({ items: [{ id: 1 }], total: 60 }) });
  await first;
  assert.equal(f.run('state.items[0].id'), 2);
  assert.equal(f.run('state.page'), 2);
});

test('gallery list requests compact card data', async () => {
  const f = fixture();
  let requested;
  f.context.fetch = async url => {
    requested = new URL(url, 'http://gallery.test/');
    return { ok: true, json: async () => ({ items: [], total: 0 }) };
  };
  f.run('renderGrid = () => {}; renderPagination = () => {};');
  await f.run('loadImages()');
  assert.equal(requested.searchParams.get('compact'), 'true');
});

test('cards show one artist while other tags remain available in details', () => {
  const f = fixture();
  f.context.picture = { id: 1, filename: 'card.jpg', rel_path: 'card.jpg', folder: '',
    media_type: 'image', rating: 'explicit', mtime: 123, suggested_folder: '',
    artists: [{name:'artist-a', zh_name:'畫師甲'}, {name:'artist-b', zh_name:'畫師乙'}],
    characters: [{name:'hidden-character'}], copyrights: [{name:'hidden-copyright'}],
    preview_tags: [{name:'hidden-general'}] };
  f.run('state.items = [picture]; renderGrid();');
  const card = f.element('image-grid').innerHTML;
  assert.equal((card.match(/class="pill artist"/g) || []).length, 1);
  assert.match(card, /畫師甲/);
  assert.match(card, /R-18/);
  assert.doesNotMatch(card, /畫師乙|hidden-character|hidden-copyright|hidden-general/);
});

test('preview delay cancels cleanly and ignores late image loads; videos use thumbnails', () => {
  const f = fixture();
  f.run("state.items = [{ id: 1, filename: 'a.jpg', media_type: 'image' }, { id: 2, filename: 'b.mp4', media_type: 'video' }]");
  f.context.targetA = { dataset: { index: '0' }, isConnected: true };
  f.context.targetB = { dataset: { index: '1' }, isConnected: true };
  f.run('scheduleHoverPreview(targetA, {x:500,y:300}); hideHoverPreview()');
  f.tick(); assert.equal(f.images.length, 0);
  f.run('scheduleHoverPreview(targetA, {x:500,y:300})'); f.tick();
  f.run('scheduleHoverPreview(targetB, {x:500,y:300})'); f.tick();
  f.images[0].onload();
  assert.equal(f.element('hover-preview').hidden, true);
  assert.equal(f.images[1].src, '/api/thumb/2');
  f.images[1].onload();
  assert.equal(f.element('hover-preview').hidden, false);
  f.run('hideHoverPreview()');
  assert.equal(f.element('hover-preview').hidden, true);
});

test('failed schedule save never launches the pipeline and preserves non-visible thresholds', async () => {
  const f = fixture();
  const requests = [];
  f.context.fetch = async (url, options) => { requests.push({ url, options }); return { ok: false, status: 500 }; };
  f.run('scheduleConfigLoaded = true; scheduleConfigSnapshot = { artist_thresh: 0.85, reverse_search_limit: 12 };');
  f.element('sch-time-1').value = '22:00';
  await f.run('runScheduleNow()');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/schedule/config');
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.artist_thresh, 0.85);
  assert.equal(payload.reverse_search_limit, 12);
  assert.match(f.element('schedule-feedback').textContent, /儲存失敗/);
});

test('successful save reports status and updating history does not overwrite an unsaved form', async () => {
  const f = fixture();
  f.context.fetch = async () => ({ ok: true, json: async () => ({ status: 'saved', config: { enabled: false } }) });
  f.run('scheduleConfigLoaded = true;');
  f.element('sch-time-1').value = '22:00';
  assert.equal(await f.run('saveScheduleConfig()'), true);
  assert.match(f.element('schedule-feedback').textContent, /已儲存/);
  f.element('sch-source-dirs').value = 'unsaved folder';
  f.run("renderScheduleHistory({ status_message: 'running', config: { source_dirs: ['saved folder'], history_logs: [] } })");
  assert.equal(f.element('sch-source-dirs').value, 'unsaved folder');
  assert.equal(f.element('sch-live-status').textContent, 'running');
});

test('disabled preview does not request an image and failure can be dismissed', () => {
  const f = fixture('{"hoverPreview":false}');
  f.context.target = { dataset: { index: '0' }, isConnected: true };
  f.run("state.items = [{id:1,filename:'a.jpg',media_type:'image'}]; scheduleHoverPreview(target,{x:1,y:1})");
  f.tick(); assert.equal(f.images.length, 0);
  f.run('browsePreferences.hoverPreview = true; scheduleHoverPreview(target,{x:1,y:1})');
  f.tick(); f.images[0].onerror();
  assert.equal(f.element('hover-preview').hidden, true);
});

test('folder selection opens the native picker at the last source and cancellation preserves sources', async () => {
  const f = fixture();
  f.element('sch-source-dirs').value = 'C:\\來源一\nD:\\來源二';
  const requests = [];
  let resolvePicker;
  f.context.fetch = (url, options) => {
    requests.push({url, options});
    return new Promise(resolve => { resolvePicker = resolve; });
  };
  const pending = f.run('openFolderBrowserModal()');
  await f.run('pickSourceFolderNative()');
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /^\/api\/schedule\/pick_folder\?/);
  assert.equal(new URL(requests[0].url, 'http://localhost').searchParams.get('initial_dir'), 'D:\\來源二');
  assert.equal(f.element('btn-pick-source-folder').disabled, true);
  assert.equal(f.element('folder-browser-modal').classList.contains('open'), false);
  resolvePicker({ok:true, json: async () => ({selected_path:''})});
  await pending;
  assert.equal(f.element('sch-source-dirs').value, 'C:\\來源一\nD:\\來源二');
  assert.equal(f.element('btn-pick-source-folder').disabled, false);
  assert.match(f.element('sch-folder-picker-status').textContent, /已取消/);
});

test('native folder selection appends Unicode paths and ignores case/slash duplicates', async () => {
  const f = fixture();
  f.run('previewSourceDirs = async () => {};');
  f.element('sch-source-dirs').value = 'C:\\Existing';
  f.context.fetch = async () => ({ok:true,json:async () => ({selected_path:'D:\\圖片\\繪師'})});
  await f.run('pickSourceFolderNative()');
  assert.equal(f.element('sch-source-dirs').value, 'C:\\Existing\nD:\\圖片\\繪師');
  f.context.fetch = async () => ({ok:true,json:async () => ({selected_path:'c:/existing/'})});
  await f.run('pickSourceFolderNative()');
  assert.equal(f.element('sch-source-dirs').value, 'C:\\Existing\nD:\\圖片\\繪師');
  assert.match(f.element('sch-folder-picker-status').textContent, /已在來源清單/);
});

test('native folder picker errors are visible and a retry remains possible', async () => {
  const f = fixture();
  f.context.fetch = async () => ({ok:false,json:async () => ({detail:'視窗啟動失敗'})});
  await f.run('pickSourceFolderNative()');
  assert.match(f.element('sch-folder-picker-status').textContent, /視窗啟動失敗/);
  assert.equal(f.element('btn-pick-source-folder').disabled, false);
  f.context.fetch = async () => ({ok:true,json:async () => ({selected_path:''})});
  await f.run('pickSourceFolderNative()');
  assert.match(f.element('sch-folder-picker-status').textContent, /已取消/);
});

test('Delete uses the current detail, all selected pages, or the focused card', () => {
  const f = fixture();
  f.run('let deleted = []; deleteImageFiles = ids => { deleted.push(ids); }; state.items = [{id:1}, {id:2}]; state.selectedIds = new Set([1,99]);');
  key(f, { key: 'Delete' });
  assert.equal(f.run('JSON.stringify(deleted)'), '[[1,99]]');
  f.element('detail-modal').classList.add('open');
  f.run('state.detailIndex = 0; state.detailImageId = 2');
  key(f, { key: 'Delete' });
  assert.equal(f.run('JSON.stringify(deleted)'), '[[1,99],[2]]');
  f.element('detail-modal').classList.remove('open');
  f.run('state.selectedIds.clear()');
  key(f, { key: 'Delete', target: { closest: selector => selector === '.card' ? { id: 'card-1' } : null } });
  assert.equal(f.run('JSON.stringify(deleted)'), '[[1,99],[2],[1]]');
  key(f, { key: 'Delete' });
  assert.equal(f.run('deleted.length'), 3, 'No implicit deletion when there is no target');
});

test('Delete ignores typing, modifiers, dialogs, repeated keys and an active request', () => {
  const f = fixture();
  f.run('let deletes = 0; deleteImageFiles = () => { deletes++; }; state.selectedIds.add(1);');
  for (const overrides of [
    { target: { closest: () => ({}) } }, { isComposing: true }, { ctrlKey: true },
    { altKey: true }, { metaKey: true }, { shiftKey: true }, { repeat: true }, { defaultPrevented: true }
  ]) key(f, { key: 'Delete', ...overrides });
  f.element('ai-modal').classList.add('open');
  key(f, { key: 'Delete' });
  f.element('ai-modal').classList.remove('open');
  f.run('imageFileOperationBusy = true');
  key(f, { key: 'Delete' });
  assert.equal(f.run('deletes'), 0);
});

test('deletion proceeds without confirm; failures preserve selection, show error notice, and permit retry', async () => {
  const f = fixture();
  f.run("state.items = [{id:1, filename:'圖片.jpg'}]; state.selectedIds.add(1)");
  let requests = 0;
  f.context.window.confirm = () => { throw Error('confirm must not be called'); };
  f.context.fetch = async () => { requests++; return { ok: false, json: async () => ({detail:'檔案使用中'}) }; };
  await f.run('deleteSelectedImages()');
  assert.equal(requests, 1);
  assert.equal(f.run('state.selectedIds.has(1)'), true);
  assert.equal(f.run('state.items.length'), 1);
  assert.equal(f.element('file-delete-notice').hidden, false);
  assert.match(f.element('file-delete-status').textContent, /檔案使用中/);
  assert.equal(f.element('btn-delete-selected').disabled, false);
});

test('successful delete updates selection and detail, unlocks before background refresh, and keeps notice hidden', async () => {
  const f = fixture();
  f.run('renderGrid = () => {}');
  let resolveRefresh;
  f.context.onRefresh = r => { resolveRefresh = r; };
  f.run('refreshDetailView = async id => { state.detailImageId = id; await new Promise(onRefresh); }');
  f.run("loadImages = loadStats = loadFolders = loadTags = async () => {}; state.items = [{id:1,filename:'a.jpg'},{id:2,filename:'b.jpg'}]; state.total = 2; state.selectedIds = new Set([1,2,99]); state.detailIndex = 0; state.detailImageId = 1;");
  f.element('detail-modal').classList.add('open');
  let resolveDelete;
  let requests = 0;
  f.context.fetch = () => { requests++; return new Promise(resolve => { resolveDelete = resolve; }); };
  const pending = f.run('deleteCurrentImage()');
  await f.run('deleteCurrentImage()');
  assert.equal(requests, 1);
  resolveDelete({ok:true,json:async () => ({deleted:1,deleted_ids:[1],recycled:1,missing_count:0})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.run('imageFileOperationBusy'), false, 'Unlocked before refreshDetailView completes');
  assert.equal(f.run('state.detailImageId'), 2);
  resolveRefresh();
  await pending;
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[2,99]');
  assert.equal(f.run('state.items[0].id'), 2);
  assert.equal(f.element('detail-modal').classList.contains('open'), true);
  assert.equal(f.element('file-delete-notice').hidden, true);
});

test('single deletion targets the displayed ID after a new copy or list reorder', async () => {
  const f = fixture();
  f.run('renderGrid = () => {}');
  f.run('refreshDetailView = async id => { state.detailImageId = id; }');
  f.run("loadImages = loadStats = loadFolders = loadTags = async () => {}; state.items = [{id:1,filename:'other.jpg'}]; state.detailIndex = 0; state.detailImageId = 7; currentDetailData = {id:7,filename:'copy.jpg'};");
  f.element('detail-modal').classList.add('open');
  let payload;
  f.context.fetch = async (_url, options) => {
    payload = JSON.parse(options.body);
    return {ok:true,json:async () => ({deleted:1,deleted_ids:[7],recycled:0,missing_count:1})};
  };
  await f.run('deleteCurrentImage()');
  assert.deepEqual(payload.image_ids, [7]);
  assert.equal(f.run('state.items[0].id'), 1);
  assert.equal(f.element('file-delete-notice').hidden, true);
});

test('missing detail renders a clear message and a removed record closes the viewer', async () => {
  const f = fixture();
  f.element('detail-modal').classList.add('open');
  f.context.fetch = async () => ({ok:true,json:async () => ({id:7,filename:'gone.jpg',file_missing:true,size_kb:1,tags:[]})});
  await f.run('refreshDetailView(7)');
  assert.match(f.element('detail-media-container').textContent, /檔案不存在/);
  assert.equal(f.run('state.detailImageId'), 7);
  f.run('loadImages = loadStats = loadFolders = loadTags = async () => {}');
  f.context.fetch = async () => ({ok:false,status:404,json:async () => ({detail:'Image not found'})});
  await f.run('refreshDetailView(7)');
  assert.equal(f.element('detail-modal').classList.contains('open'), false);
  assert.equal(f.run('currentDetailData'), null);
});

test('delete removes cards before refill and old or stale responses cannot bring them back', async () => {
  const f = fixture();
  f.run("renderGrid = () => {}; loadStats = loadFolders = loadTags = async () => {}; state.items = [{id:1,filename:'a.jpg'},{id:2,filename:'b.jpg'}]; state.total = 2; state.selectedIds.add(1)");
  const pending = [];
  f.context.fetch = (url, options) => {
    if (url === '/api/images/delete') return Promise.resolve({ok:true,json:async () => ({deleted:1,deleted_ids:[1],recycled:1})});
    assert.equal(options.cache, 'no-store');
    return new Promise(resolve => pending.push(resolve));
  };
  const old = f.run('loadImages()');
  const deletion = f.run('deleteSelectedImages()');
  while (pending.length < 2) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.run('state.items.map(item => item.id).join()'), '2', 'Card disappears while refill is pending');
  assert.equal(f.element('file-delete-notice').hidden, true, 'Notice stays hidden on normal deletion');
  pending[0]({ok:true,json:async () => ({items:[{id:1},{id:2}],total:2})});
  await old;
  assert.equal(f.run('state.items.map(item => item.id).join()'), '2');
  pending[1]({ok:true,json:async () => ({items:[{id:1},{id:2}],total:2})});
  while (pending.length < 3) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.run('state.items.map(item => item.id).join()'), '2', 'Stale post-delete response is filtered');
  pending[2]({ok:true,json:async () => ({items:[{id:2}],total:1})});
  await deletion;
  assert.equal(f.run('state.total'), 1);
});

test('detail deletion advances across page boundaries, falls back to previous, and closes only when empty', async () => {
  const scenarios = [
    { items:[1,2], id:2, page:1, total:2, nextItems:[1], expected:1 },
    { items:[1], id:1, page:1, total:1, nextItems:[], expected:null },
    { items:[1,2,3,4,5,6,7,8,9,10], id:10, page:1, total:11, nextItems:[1,2,3,4,5,6,7,8,9,11], expected:11 },
    { items:[11], id:11, page:2, total:11, nextItems:[1,2,3,4,5,6,7,8,9,10], expected:10 },
  ];
  for (const scenario of scenarios) {
    const f = fixture();
    f.context.scenario = scenario;
    f.run("renderGrid = () => {}; loadStats = loadFolders = loadTags = async () => {}; state.items = scenario.items.map(id => ({id,filename:id+'.jpg'})); state.total = scenario.total; state.page = scenario.page; state.pageSize = 10; state.detailImageId = scenario.id; refreshDetailView = async id => { state.detailImageId = id; }; loadImages = async () => { state.items = scenario.nextItems.map(id => ({id,filename:id+'.jpg'})); state.total = scenario.total - 1; return true; };");
    f.element('detail-modal').classList.add('open');
    f.context.fetch = async () => ({ok:true,json:async () => ({deleted:1,deleted_ids:[scenario.id],recycled:1})});
    await f.run('deleteCurrentImage()');
    assert.equal(f.run('state.detailImageId'), scenario.expected);
    assert.equal(f.element('detail-modal').classList.contains('open'), scenario.expected !== null);
  }
});

test('Shift selects a forward or backward range in display order and preserves other selections', () => {
  const f = fixture();
  f.run('state.items = [8,2,9,4].map(id => ({id})); state.selectedIds.add(99); toggleSelect(2); toggleSelect(4, {shiftKey:true});');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[99,2,9,4]');
  f.run('toggleSelect(8, {shiftKey:true})');
  assert.equal(f.run('state.selectedIds.has(8)'), true);
  f.run('clearSelection(); toggleSelect(4); toggleSelect(2, {shiftKey:true})');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[4,2,9]');
});

test('Shift without a visible anchor selects only the target and normal click still toggles', () => {
  const f = fixture();
  f.run('state.items = [{id:1}]; toggleSelect(1); state.items = [{id:4},{id:5}]; toggleSelect(5, {shiftKey:true});');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1,5]');
  f.run('toggleSelect(5)');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1]');
});

test('paint segment intersection catches fast sweeps without selecting unrelated corners', () => {
  const f = fixture();
  assert.equal(f.run('selectionSegmentHitsRect({x:0,y:10},{x:100,y:10},{left:40,right:60,top:0,bottom:20})'), true);
  assert.equal(f.run('selectionSegmentHitsRect({x:100,y:10},{x:0,y:10},{left:40,right:60,top:0,bottom:20})'), true);
  assert.equal(f.run('selectionSegmentHitsRect({x:0,y:0},{x:100,y:100},{left:0,right:20,top:80,bottom:100})'), false);
  assert.equal(f.run('selectionSegmentHitsRect({x:5,y:5},{x:5,y:5},{left:0,right:10,top:0,bottom:10})'), true);
});

test('paint can reverse over visited cards without flickering while still inside one card', () => {
  const f = fixture();
  f.element('gallery-scroll').getBoundingClientRect = () => ({left:0,right:100,top:0,bottom:20});
  f.element('image-grid').querySelectorAll = () => Array.from({length:5}, (_, index) => ({
    id:`card-${index+1}`, getBoundingClientRect:() => ({left:index*10,right:index*10+8,top:0,bottom:10})
  }));
  f.run('state.items = [1,2,3,4,5].map(id => ({id})); state.selectedIds.add(1); selectionPointer = {active:true,select:true,last:{x:5,y:5},visited:new Set([1]),inside:new Set([1])}; paintSelectionTo({x:45,y:5});');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1,2,3,4,5]');
  f.run('paintSelectionTo({x:25,y:5})');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1,2,5]');
  f.run('paintSelectionTo({x:26,y:6}); paintSelectionTo({x:25,y:5})');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1,2,5]');
  f.run('paintSelectionTo({x:45,y:5})');
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[1,2,4]');
});
