// Run with: node --test tests/webui.test.cjs
// Exercise the shipped script with isolated browser stubs; never call the live scheduler.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../static/gallery-app.js'), 'utf8');

test('tag selection updates the cached sidebar without another tag request', async () => {
  const f = fixture();
  let requests = 0;
  f.context.fetch = async () => {
    requests++;
    return { json: async () => [{name:'artist-a', zh_name:'畫師甲', category:'artist', count:10}] };
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
  pending[1]({json: async () => [{name:'current', category:'artist', count:10}]});
  await current;
  pending[0]({json: async () => [{name:'outdated', category:'general', count:20}]});
  await old;
  assert.match(f.element('sidebar-tag-list').innerHTML, /current/);
  assert.doesNotMatch(f.element('sidebar-tag-list').innerHTML, /outdated/);
});

function fixture(saved = '{}') {
  const elements = new Map(), timers = new Map(), images = [];
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        value: '', checked: false, hidden: true, disabled: false, textContent: '', innerHTML: '', style: {},
        classList: { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
          toggle(c, value) { if (value) classes.add(c); else classes.delete(c); } },
        setAttribute() {}, removeAttribute() {}, reportValidity: () => true,
        focus() {}, getClientRects: () => [1]
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
  assert.equal(f.run("state.includeTags.join(',')"), 'b');
  f.run("toggleIncludeTag('a')");
  assert.equal(f.run("state.includeTags.join(',')"), 'b,a');
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
  f.run('state.detailIndex = 1');
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

test('cancelled deletion sends no request; failures preserve selection and permit retry', async () => {
  const f = fixture();
  f.run("state.items = [{id:1, filename:'圖片.jpg'}]; state.selectedIds.add(1)");
  let requests = 0;
  f.context.window.confirm = () => false;
  f.context.fetch = async () => { requests++; return { ok: false, json: async () => ({detail:'檔案使用中'}) }; };
  await f.run('deleteSelectedImages()');
  assert.equal(requests, 0);
  f.context.window.confirm = () => true;
  await f.run('deleteSelectedImages()');
  assert.equal(requests, 1);
  assert.equal(f.run('state.selectedIds.has(1)'), true);
  assert.equal(f.run('state.items.length'), 1);
  assert.match(f.element('file-delete-status').textContent, /檔案使用中/);
  assert.equal(f.element('btn-delete-selected').disabled, false);
});

test('successful delete updates selection and detail, prevents duplicate submits, and supports restore', async () => {
  const f = fixture();
  f.run("loadImages = loadStats = loadFolders = loadTags = async () => {}; state.items = [{id:1,filename:'a.jpg'},{id:2,filename:'b.jpg'}]; state.total = 2; state.selectedIds = new Set([1,2,99]); state.detailIndex = 0;");
  f.element('detail-modal').classList.add('open');
  f.context.window.confirm = () => true;
  let resolveDelete;
  let requests = 0;
  f.context.fetch = () => { requests++; return new Promise(resolve => { resolveDelete = resolve; }); };
  const pending = f.run('deleteCurrentImage()');
  await f.run('deleteCurrentImage()');
  assert.equal(requests, 1);
  resolveDelete({ok:true,json:async () => ({deleted:1,deleted_ids:[1],operation_id:'delete_' + 'a'.repeat(32)})});
  await pending;
  assert.equal(f.run('JSON.stringify([...state.selectedIds])'), '[2,99]');
  assert.equal(f.run('state.items[0].id'), 2);
  assert.equal(f.element('detail-modal').classList.contains('open'), false);
  assert.equal(f.element('btn-restore-deleted').hidden, false);
  f.context.fetch = async () => ({ok:true,json:async () => ({restored:1})});
  await f.run('restoreLastDeletedImages()');
  assert.match(f.element('file-delete-status').textContent, /已復原 1 個檔案/);
  assert.equal(f.element('btn-restore-deleted').hidden, true);
});
