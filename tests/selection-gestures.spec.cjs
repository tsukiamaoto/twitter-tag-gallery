/* Pointer/keyboard verification uses mocked images and never changes the real library. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const original = Array.from({length:24}, (_, i) => ({id:i+1, filename:`image_${i+1}.jpg`, rel_path:`image_${i+1}.jpg`,
  folder:'', media_type:'image', mtime:123, size_kb:1, artists:[], characters:[], copyrights:[], preview_tags:[], tags:[]}));

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    // Keep five columns for the horizontal paint scenarios, including the workspace rail.
    const context = await browser.newContext({viewport:{width:1536,height:1000}, reducedMotion:'reduce'});
    await context.addInitScript(() => localStorage.setItem('twitter-tag-gallery.browse.v1', '{"pageSize":10}'));
    let items = [...original];
    const writes = [], errors = [];
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      if (endpoint === '/') return route.fulfill({contentType:'text/html',body:html});
      if (endpoint.startsWith('/static/')) return route.fulfill({contentType:endpoint.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,endpoint),'utf8')});
      if (/^\/api\/(thumb|raw)\//.test(endpoint)) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="teal"/></svg>'});
      let data = {};
      const ai = {running:false,model_loaded:false,processed_count:0};
      if (request.method() !== 'GET') {
        assert.equal(endpoint, '/api/images/delete', 'Selection itself must never write');
        const payload = request.postDataJSON(); writes.push(payload);
        items = items.filter(item => !payload.image_ids.includes(item.id));
        data = {deleted:payload.image_ids.length,deleted_ids:payload.image_ids,restorable:0,missing_count:0};
      } else if (endpoint === '/api/images') {
        const size = Number(url.searchParams.get('page_size')), page = Number(url.searchParams.get('page'));
        data = {items:items.slice((page-1)*size,page*size),total:items.length};
      } else if (endpoint.startsWith('/api/image/')) data = items.find(item => item.id === Number(endpoint.split('/').pop()));
      else if (endpoint === '/api/stats') data = {total_images:items.length,root_unorganized:items.length,folder_organized:0,ai_tagged:0,suggested_folder_count:0,ai_status:ai};
      else if (endpoint === '/api/folders') data = [];
      else if (endpoint === '/api/tags') data = {items:[], next_offset:0, has_more:false};
      else if (endpoint === '/api/runtime') data = {ai,reverse:ai,schedule:{config:{}},active_job:{},jobs:[],model:{}};
      else if (endpoint.endsWith('/status')) data = ai;
      return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://gallery.test/');
    await page.waitForSelector('#card-10');
    const selected = () => page.locator('.card.selected').evaluateAll(cards => cards.map(card => Number(card.id.slice(5))));
    const clear = async () => { if (await page.locator('#batch-bar').isVisible()) await page.locator('#batch-bar button[onclick="clearSelection()"]').click(); };
    async function point(id, checkbox = false) {
      const box = await page.locator(`#card-${id} ${checkbox?'.card-check':'.thumb-box'}`).boundingBox();
      return {x:box.x+box.width/2,y:box.y+box.height/2};
    }
    async function sweep(from, to, checkbox = false, reverse = false) {
      const a = await point(from,checkbox), b = await point(to,checkbox);
      await page.mouse.move(a.x,a.y); await page.mouse.down();
      await page.mouse.move(b.x,b.y,{steps:1});
      if (reverse) await page.mouse.move(a.x,a.y,{steps:1});
      await page.mouse.up();
    }

    await page.locator('#card-2 .card-check').click();
    await page.locator('#card-6 .card-check').click({modifiers:['Shift']});
    assert.deepEqual(await selected(), [2,3,4,5,6]);
    await page.locator('#card-8 .thumb-box').click({modifiers:['Shift']});
    assert.deepEqual(await selected(), [2,3,4,5,6,7,8]);
    assert.equal(await page.locator('#detail-modal').isVisible(), false);
    assert.equal(await page.locator('#card-6 .card-check').getAttribute('aria-checked'), 'true');
    await clear();
    await page.locator('#card-8 .card-check').click();
    await page.locator('#card-3 .card-check').click({modifiers:['Shift']});
    assert.deepEqual(await selected(), [3,4,5,6,7,8]);

    await clear();
    await page.locator('#card-2 .card-check').click();
    await page.locator('#card-6 .card-check').focus();
    await page.keyboard.press('Shift+Space');
    assert.deepEqual(await selected(), [2,3,4,5,6]);
    await page.keyboard.press('Space');
    assert.deepEqual(await selected(), [2,3,4,5], 'Space toggles once');

    await clear();
    const firstCard = await page.locator('#card-1').elementHandle();
    const a = await point(1,true), b = await point(5,true);
    const top = (await page.locator('#card-1').boundingBox()).y;
    await page.mouse.move(a.x,a.y); await page.mouse.down();
    await page.mouse.move(b.x,b.y,{steps:1});
    assert.deepEqual(await selected(), [1,2,3,4,5], 'A fast sweep includes intermediate cards');
    assert.equal((await page.locator('#card-1').boundingBox()).y, top, 'Selection bar does not move the held row');
    assert.equal(await firstCard.evaluate(card => card.isConnected), true, 'Paint updates cards in place');
    await page.mouse.up();
    assert.equal(await page.locator('#detail-modal').isVisible(), false);
    assert.equal(await page.locator('#batch-count').textContent(), '5');
    await page.locator('#card-8 .card-check').click();
    assert.deepEqual(await selected(), [1,2,3,4,5,8], 'The next ordinary click is not suppressed');
    await sweep(1,5,true,true);
    assert.deepEqual(await selected(), [1,2,3,4,8], 'Sweeping back into cards toggles them again');

    await clear();
    const forwardStart = await point(1), forwardEnd = await point(5), middle = await point(3);
    await page.mouse.move(forwardStart.x,forwardStart.y); await page.mouse.down();
    await page.mouse.move(forwardEnd.x,forwardEnd.y,{steps:1});
    assert.deepEqual(await selected(), [1,2,3,4,5]);
    await page.mouse.move(middle.x,middle.y,{steps:1});
    assert.deepEqual(await selected(), [1,2,5], 'Returning over cards 3 and 4 cancels their selection');
    await page.mouse.move(middle.x+5,middle.y+3,{steps:5});
    assert.deepEqual(await selected(), [1,2,5], 'Moving inside a card does not repeatedly toggle it');
    await page.mouse.move(forwardEnd.x,forwardEnd.y,{steps:1});
    assert.deepEqual(await selected(), [1,2,4], 'A third pass can select or cancel the same cards again');
    await page.mouse.up();
    assert.equal(await page.locator('#batch-count').textContent(), '3');
    assert.equal(await page.locator('#detail-modal').isVisible(), false);

    await clear();
    await sweep(5,1);
    assert.deepEqual(await selected(), [1,2,3,4,5], 'Painting may start on the image and run backwards');
    assert.equal(await page.locator('#detail-modal').isVisible(), false);
    await page.locator('#card-9 .thumb-box').click();
    await page.waitForFunction(() => document.querySelector('#dt-filename').textContent === 'image_9.jpg');
    await page.keyboard.press('Escape');

    await clear();
    const c = await point(1), d = await point(5);
    await page.mouse.move(c.x,c.y); await page.mouse.down({button:'right'});
    await page.mouse.move(d.x,d.y); await page.mouse.up({button:'right'});
    assert.deepEqual(await selected(), [], 'Right drag does not select');
    await page.keyboard.press('Escape');
    const releaseFrom = await point(1), releaseTo = await point(5);
    await page.mouse.move(releaseFrom.x,releaseFrom.y); await page.mouse.down();
    await page.mouse.move(releaseTo.x,releaseTo.y);
    await page.mouse.move(1400,30); await page.mouse.up();
    const afterRelease = await selected();
    const untouched = await point(8);
    await page.mouse.move(untouched.x,untouched.y);
    assert.deepEqual(await selected(), afterRelease, 'Release outside the grid stops painting');
    await clear();
    await page.locator('#card-1 .card-check').click();
    await page.locator('#btn-next').click();
    await page.waitForSelector('#card-11');
    await page.locator('#card-15 .card-check').click({modifiers:['Shift']});
    assert.deepEqual(await selected(), [15], 'An anchor on another page does not select an unintended range');
    assert.equal(await page.locator('#batch-count').textContent(), '2', 'Other pages keep their selected IDs');
    assert.deepEqual(writes, []);

    await page.setViewportSize({width:768,height:1000});
    await clear();
    await page.locator('#card-11 .card-check').click();
    await page.locator('#card-16 .thumb-box').click({modifiers:['Shift']});
    assert.deepEqual(await selected(), [11,12,13,14,15,16]);
    await page.locator('#btn-delete-selected').click();
    await page.waitForFunction(() => !document.querySelector('#card-11'));
    assert.equal(await page.locator('#file-delete-notice').isHidden(), true);
    assert.deepEqual(writes, [{image_ids:[11,12,13,14,15,16]}]);
    assert.equal(await page.locator('#card-11').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: Shift ranges, keyboard, fast and reverse paint, deselection, normal clicks, page boundaries, responsive layout and batch-delete handoff');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
