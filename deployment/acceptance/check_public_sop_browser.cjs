/** Live public-guidance acceptance. Requires an explicitly selected deployed URL. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
// The UI names the release browser harness also asserts, so a rename fails the deploy gate first.
const N = require('../../ui/scripts/browser/ui-names.cjs');
const root = path.resolve(__dirname, '../..');
const url = process.env.WFM_PUBLIC_URL;
if (!url || new URL(url).protocol !== 'https:') throw Error('Set WFM_PUBLIC_URL to the HTTPS deployment to test.');
const out = path.resolve(process.env.WFM_BROWSER_OUTPUT || path.join(root, '.runtime/verification/public-sop-browser'));
fs.mkdirSync(out, { recursive: true });
const collection = path.join(root, 'backend/knowledge/public-sop/documents');
const normalize = value => value.replace(/\s+/g, ' ').trim();
const expected = Object.fromEntries(fs.readdirSync(collection).filter(n => n.endsWith('.txt')).map(name => {
  const metadata = JSON.parse(fs.readFileSync(path.join(collection, name + '.metadata.json'))).metadataAttributes;
  return [metadata.title, { ...metadata, excerpt: normalize(fs.readFileSync(path.join(collection, name), 'utf8').slice(0, 500)) }];
}));

(async () => {
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || 'chrome',
    headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
    page = await context.newPage();
    const errors = [], requests = [], direct = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const target = new URL(request.url());
      if (target.pathname === '/simulation-api/v1/investigation/turns') requests.push(request.postDataJSON());
      if (target.hostname.includes('bedrock-agentcore')) direct.push(target.hostname);
    });
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    const answer = page.getByRole('article', { name: N.AGENT_MESSAGE, exact: true });
    const before = await answer.count();
    await page.getByLabel(N.CHAT_INPUT, { exact: true }).fill(
      'Use the public guidance collection for this planning exercise. Give two brief considerations for household evacuation routes and transport preparation, and two for firefighter escape routes, safety zones and crew communications. Keep the answer under 250 words. Cite the official publications. Distinguish guidance from incident orders.');
    await page.getByRole('button', { name: N.SEND_MESSAGE, exact: true }).click();
    await page.waitForFunction(([name, n]) => [...document.querySelectorAll('article')]
      .filter(article => article.getAttribute('aria-label') === name).length > n,
    [N.AGENT_MESSAGE, before], { timeout: 240000 });
    await page.getByRole('status', { name: N.PROCESSING, exact: true }).waitFor({ state: 'hidden', timeout: 240000 });
    const text = await answer.last().innerText();
    fs.writeFileSync(path.join(out, 'live-answer.txt'), text);
    assert.match(text, /evacuat/i);
    assert.match(text, /escape route/i);
    assert.match(text, /communicat/i);
    assert.doesNotMatch(text, /procedure guidance is unavailable|ended without a completed|connection failed/i);
    await answer.last().getByRole('button', { name: N.INSPECT_EVIDENCE, exact: true }).click();
    const panel = page.locator('aside.detail-panel-wrapper');
    await panel.waitFor({ state: 'visible' });
    const articles = panel.locator('details.citation-item');
    assert.ok(await articles.count(), 'Source articles use folded disclosure controls');
    assert.equal(await panel.locator('details.citation-item[open]').count(), 0);
    for (const item of await articles.all()) await item.locator('summary').click();
    const links = panel.locator('.citation-item a');
    await links.first().click({ trial: true }); // Verifies the visible drawer, including its animation/stacking.
    const hrefs = await links.evaluateAll(items => items.map(item => item.href));
    assert.ok(hrefs.some(href => href.startsWith('https://www.ready.gov/')));
    assert.ok(hrefs.some(href => href.startsWith('https://www.nps.gov/')));
    const sourceText = await panel.innerText();
    assert.doesNotMatch(sourceText, /Wildland Fire Incident Response Guide|s3:\/\//);
    fs.writeFileSync(path.join(out, 'live-source-panel.txt'), sourceText);
    const citations = [];
    for (const item of await panel.locator('.citation-item').all()) {
      const title = (await item.locator('.citation-document').innerText()).trim();
      const source = expected[title];
      assert.ok(source, 'Every displayed source must belong to the reviewed manifest');
      const excerpt = await item.locator('.citation-excerpt').innerText();
      assert.equal(normalize(excerpt.slice(1, -1)), source.excerpt, 'Excerpt must match pinned source text verbatim');
      assert.equal(await item.locator('a').getAttribute('href'), source.source_url);
      const details = await item.innerText();
      assert.ok(details.includes(source.publisher) && details.includes(source.retrieved_at));
      if (source.source_date) assert.ok(details.includes(source.source_date));
      citations.push({ title, url: source.source_url, excerpt_matches_source: true });
    }
    const cookie = (await context.cookies()).find(value => value.name === '__Host-wildfire-demo');
    assert.ok(cookie?.secure && cookie.httpOnly);
    assert.equal(requests.length, 1);
    assert.equal(direct.length, 0);
    assert.equal(errors.length, 0);
    await page.screenshot({ path: path.join(out, 'live-public-sop.png'), fullPage: true, animations: 'disabled' });
    fs.writeFileSync(path.join(out, 'live-public-sop-acceptance.json'), JSON.stringify({
      status: 'passed', url, citations,
      checks: ['fresh public cookie session', 'same-origin broker; no browser AgentCore call',
        'real public answer covers evacuation and crew safety', 'official hyperlinks visible and actionable',
        'citation excerpts match reviewed source text verbatim', 'publisher and dates match source metadata',
        'no internal S3 paths in the sources drawer', 'no browser runtime errors'],
    }, null, 2));
    console.log('Live public SOP Investigation and source integrity passed');
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(out, 'live-sop-failure.png'), fullPage: true, animations: 'disabled' });
      fs.writeFileSync(path.join(out, 'live-sop-failure.txt'), await page.locator('body').innerText());
    }
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
