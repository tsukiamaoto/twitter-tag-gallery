const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/gallery-app.js'), 'utf8');
const key = 'twitter-tag-gallery.tag-filters.v1';
const browseKey = 'twitter-tag-gallery.browse.v1';

function fixture(saved = null, storageOverrides = {}) {
  const storage = new Map([[key, saved], [browseKey, '{"pageSize":37}']]);
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {value:'', hidden:false, textContent:'', innerHTML:'',
          setAttribute() {}, classList:{toggle() {}}});
        return elements.get(id);
      },
      querySelectorAll: () => [], addEventListener() {}
    },
    window: {addEventListener() {}},
    localStorage: {getItem: name => storage.get(name) ?? null, setItem: (name, value) => storage.set(name, value), ...storageOverrides},
    URLSearchParams, AbortController, setTimeout, clearTimeout
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run('loadImages = () => {}; loadTags = () => {};');
  return {run, elements, storage, saved: () => storage.get(key)};
}

test('saved tag filters validate data and preserve an explicitly empty selection', () => {
  for (const saved of [null, 'broken json', 'null', '[]', '{"includeTags":"artist"}']) {
    const f = fixture(saved);
    assert.equal(f.run('state.includeTags.join(",")'), '最近');
    assert.equal(f.run('state.tagCategory'), 'recent');
  }
  const f = fixture(JSON.stringify({includeTags:['artist', null, '', ' ', 7, 'artist', 'night'],
    tagCategory:'invalid', labels:{artist:'畫師甲', night:42, unused:'忽略'}}));
  assert.equal(f.run('state.includeTags.join(",")'), 'artist,night');
  assert.equal(f.run('state.tagCategory'), 'recent');
  assert.equal(f.run('tagLabels.get("artist")'), '畫師甲');
  assert.equal(f.run('tagLabels.size'), 1);
  assert.equal(fixture('{"includeTags":[],"tagCategory":"general"}').run('state.includeTags.length'), 0);
});

test('tag choices, category and canonical labels survive reload independently of browse preferences', () => {
  const f = fixture();
  f.run(`clearTagFilters(); setTagCategory('artist');
    renderTagList([{name:'artist', zh_name:'現有繪師：畫師甲', category:'artist', count:2, aliases:['artist','alias']},
      {name:'night', zh_name:'夜景', category:'general', count:1}]);
    toggleIncludeTag('alias'); toggleIncludeTag('night'); toggleIncludeTag('night');`);
  assert.deepEqual(JSON.parse(f.saved()).includeTags, ['artist']);
  const reloaded = fixture(f.saved());
  assert.equal(reloaded.run('state.pageSize'), 37);
  assert.equal(reloaded.run('state.includeTags.join(",")'), 'artist');
  assert.equal(reloaded.run('state.tagCategory'), 'artist');
  reloaded.run('renderActiveFilters()');
  assert.match(reloaded.elements.get('active-filters-bar').innerHTML, /畫師甲/);
  assert.equal(f.storage.get(browseKey), '{"pageSize":37}');
});

test('clearing tags or all filters stays cleared after reload; recent-category changes are saved', () => {
  for (const clear of ['clearTagFilters', 'clearAllFilters']) {
    const f = fixture();
    f.run(`toggleIncludeTag('artist'); ${clear}();`);
    assert.equal(fixture(f.saved()).run('state.includeTags.length'), 0);
    f.run("setTagCategory('recent')");
    const reloaded = fixture(f.saved());
    assert.equal(reloaded.run('state.includeTags.join(",")'), '最近');
    assert.equal(reloaded.run('state.tagCategory'), 'recent');
  }
});

test('tag filtering still works when local storage cannot be read or written', () => {
  const f = fixture(null, {
    getItem() { throw Error('blocked'); }, setItem() { throw Error('quota exceeded'); }
  });
  f.run("clearTagFilters(); setTagCategory('general'); toggleIncludeTag('night');");
  assert.equal(f.run('state.includeTags.join(",")'), 'night');
  assert.equal(f.run('state.tagCategory'), 'general');
  f.run('clearAllFilters()');
  assert.equal(f.run('state.includeTags.length'), 0);
});
