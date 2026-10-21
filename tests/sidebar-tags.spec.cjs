/* Scroll pagination uses mocked APIs and media; never mutates the live gallery. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const tags = Array.from({length:125}, (_, i) => ({id:i+1, name:`tag-${String(i).padStart(3,'0')}`,
  zh_name:`標籤 ${i}`, category:'general', count:1000-i}));
const artists = [{id:1000,name:'artist',zh_name:'畫師',category:'artist',count:5,aliases:['artist','artist-alias']}];
const item = {id:1,filename:'sample.jpg',rel_path:'sample.jpg',folder:'',mtime:123,media_type:'image',artists:[],characters:[],copyrights:[],preview_tags:[]};
const media = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="teal"/></svg>';

(async () => {
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const requests = [], errors = [], writes = [];
    let failNext = false, delayNext = false, releaseDelayed = null;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (request.method() !== 'GET') writes.push(endpoint);
      if (endpoint === '/') return route.fulfill({contentType:'text/html',body:html});
      if (endpoint.startsWith('/static/')) return route.fulfill({contentType:endpoint.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,endpoint),'utf8')});
      if (/^\/api\/(raw|thumb)\//.test(endpoint)) return route.fulfill({contentType:'image/svg+xml',body:media});
      const ai = {running:false,model_loaded:false,processed_count:0};
      let data = {};
      if (endpoint === '/api/tags') {
        const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
        const q = url.searchParams.get('q'), category = url.searchParams.get('category');
        assert.equal(url.searchParams.get('paged'), 'true');
        requests.push({offset,limit,q,category});
        if (offset === 40 && failNext) { failNext = false; return route.fulfill({status:503,body:'{}'}); }
        if (offset === 40 && delayNext) {
          delayNext = false;
          await new Promise(resolve => { releaseDelayed = resolve; });
        }
        const all = category === 'artist' ? artists : tags;
        const matching = all.filter(tag => !q || tag.name.includes(q) || tag.zh_name.includes(q));
        const items = matching.slice(offset,offset+limit);
        data = {items,next_offset:offset+items.length,has_more:offset+items.length<matching.length};
      } else if (endpoint === '/api/images') data = {items:[item],total:1};
      else if (endpoint === '/api/stats') data = {total_images:1,root_unorganized:1,folder_organized:0,ai_tagged:0,suggested_folder_count:0,ai_status:ai};
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/runtime') data = {ai,reverse:ai,schedule:{config:{}},active_job:{},jobs:[],model:{}};
      else if (endpoint.endsWith('/status')) data = ai;
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    const list = page.locator('#sidebar-tag-list'), rows = list.locator('.tag-item');
    const ready = count => page.waitForFunction(count => document.querySelectorAll('#sidebar-tag-list .tag-item').length === count && document.querySelector('#sidebar-tag-list').getAttribute('aria-busy') === 'false', count);
    const bottom = () => list.evaluate(node => { node.scrollTop = node.scrollHeight; });
    await ready(40);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(requests.length,1,'Only the first batch is requested before scrolling');
    assert.equal(requests[0].limit,40);
    await rows.first().locator('button').click();
    const firstNode = await rows.first().elementHandle();
    await bottom();
    const oldScroll = await list.evaluate(node => node.scrollTop);
    await ready(80);
    assert.equal(await firstNode.evaluate(node => node.isConnected),true,'Appending keeps existing DOM nodes');
    assert.ok(Math.abs(await list.evaluate(node => node.scrollTop)-oldScroll)<2,'Appending preserves scroll position');
    assert.equal(await rows.first().locator('button').getAttribute('aria-pressed'),'true');
    await bottom(); await ready(120);
    await bottom(); await ready(125);
    const finishedRequests = requests.length;
    await bottom();
    assert.equal(await page.locator('#tag-list-status').isVisible(),false);
    assert.equal(new Set(await rows.locator('button').evaluateAll(nodes=>nodes.map(n=>n.title))).size,125);
    assert.equal(requests.length,finishedRequests,'The completed list stops requesting pages');

    await page.locator('.cat-pill[data-cat="all"]').click(); await ready(40);
    failNext = true;
    await bottom();
    const retry = page.getByRole('button',{name:'載入失敗，點此重試'});
    await retry.waitFor({state:'visible'});
    assert.equal(await rows.count(),40,'A failed page keeps previously loaded tags');
    await retry.click(); await ready(80);
    assert.deepEqual(requests.slice(-2).map(r=>r.offset),[40,40]);

    await page.locator('.cat-pill[data-cat="general"]').click(); await ready(40);
    delayNext = true;
    await bottom();
    await page.waitForFunction(() => document.querySelector('#sidebar-tag-list').getAttribute('aria-busy') === 'true');
    await page.locator('#tag-search-input').fill('tag-124'); await ready(1);
    assert.equal(await rows.first().locator('button').getAttribute('title'),'加入篩選：標籤 124');
    releaseDelayed();
    await page.evaluate(() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await rows.count(),1,'A cancelled older page cannot join search results');
    await page.locator('#tag-search-input').fill(''); await ready(40);
    assert.equal(await rows.first().locator('button').getAttribute('aria-pressed'),'true','Selection survives search changes');
    await page.locator('.cat-pill[data-cat="artist"]').click(); await ready(1);
    assert.equal(await rows.first().locator('.tag-label-zh').textContent(),'畫師');

    await page.setViewportSize({width:390,height:700});
    await page.locator('#shell-open-filters').click();
    await page.locator('.cat-pill[data-cat="general"]').click(); await ready(40);
    await bottom(); await ready(80);
    fs.mkdirSync(path.join(root,'.tag_gallery/ui-review'),{recursive:true});
    await page.screenshot({path:path.join(root,'.tag_gallery/ui-review/sidebar-tags-mobile.png')});
    assert.deepEqual(errors,[]);
    assert.deepEqual(writes,[]);
    console.log(JSON.stringify({result:'PASS',tagRequests:requests.length,initialBatch:40,allTags:125,desktopAndMobile:true,browserErrors:0}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
