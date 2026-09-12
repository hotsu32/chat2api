// Discover the real DOM contract before trusting any selector.
//
// The previous probe hard-coded `[data-message-author-role="assistant"]` and
// the `*-turn-action-button` testids. When a selector misses, the probe cannot
// tell "the product did not render this" from "I asked for the wrong thing" --
// and the earlier report's "assistant showed 8 chars then no node" is exactly
// the shape a wrong selector produces. This script makes no assumptions: it
// opens a settled conversation and dumps the attribute vocabulary actually
// present, so the matrix probes can be pointed at real selectors.
//
// Zero generations: it only reads an existing conversation.
//
// Usage: node discover_dom.js <seed> <convId> <label>
const {chromium} = require('/Users/Zhuanz/.codex/skills/playwright-skill/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const L = require('./probe_lib.js');

const BASE = 'http://127.0.0.1:5025';
const OUT = process.env.M5_OUT || path.join(__dirname, '..', '..', 'tmp/agent-team/m5-acceptance/evidence');
const NAV_TIMEOUT = 60000;
const SETTLE_MS = 12000;

const SURVEY = () => {
  const out = {
    counts: {},
    testids: {},
    messageAttrs: [],
    buttonInventory: [],
    candidateMessageSelectors: [],
  };

  // Which of the historically-used selectors exist at all?
  const probes = [
    '[data-message-author-role]',
    '[data-message-author-role="assistant"]',
    '[data-message-id]',
    '[data-testid^="conversation-turn"]',
    'article',
    '.markdown',
    '[data-start]',
    'main',
  ];
  for (const sel of probes) out.counts[sel] = document.querySelectorAll(sel).length;

  // Full data-testid vocabulary with occurrence counts -- this is what tells us
  // the real names of the action buttons without guessing.
  for (const el of document.querySelectorAll('[data-testid]')) {
    const id = el.getAttribute('data-testid');
    out.testids[id] = (out.testids[id] || 0) + 1;
  }

  // Every attribute present on message-ish containers, so we can find whatever
  // identity attribute this build actually uses.
  const msgs = document.querySelectorAll('[data-message-author-role], [data-testid^="conversation-turn"], article');
  for (const el of Array.from(msgs).slice(-4)) {
    out.messageAttrs.push({
      tag: el.tagName.toLowerCase(),
      attrs: Array.from(el.attributes).map((a) => a.name),
      // Values only for attributes that cannot carry content: ids and roles.
      role: el.getAttribute('data-message-author-role'),
      testid: el.getAttribute('data-testid'),
      hasMessageId: el.hasAttribute('data-message-id'),
      textLen: (el.innerText || '').trim().length,
    });
  }

  // Buttons near the end of the conversation: name, testid, aria-label. These
  // are the M3 "bottom action bar" candidates.
  const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
  for (const el of btns) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    out.buttonInventory.push({
      testid: el.getAttribute('data-testid'),
      ariaLabel: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      textLen: (el.innerText || '').trim().length,
      y: Math.round(r.top),
    });
  }
  out.buttonInventory.sort((a, b) => a.y - b.y);

  // Anything that looks like it holds assistant prose, ranked by text length,
  // as a fallback identity anchor if no role attribute exists in this build.
  const scored = Array.from(document.querySelectorAll('main div, main article'))
    .map((el) => ({
      el,
      len: (el.innerText || '').trim().length,
      testid: el.getAttribute('data-testid'),
      cls: (el.className || '').toString().slice(0, 60),
    }))
    .filter((x) => x.len > 40)
    .sort((a, b) => a.len - b.len);   // smallest container holding real text
  out.candidateMessageSelectors = scored.slice(0, 6)
    .map((x) => ({testid: x.testid, cls: x.cls, len: x.len}));

  out.url = location.pathname;
  out.bodyTextLen = (document.body.innerText || '').trim().length;
  return out;
};

(async () => {
  const [seed, convId, label] = process.argv.slice(2);
  const rec = {kind: 'dom_discovery', label, seed_alias: seed,
               conv_ref: convId ? convId.slice(0, 8) + '...' : null,
               checkedAt: new Date().toISOString(), stage: 'start', generationsUsed: 0};
  const net = [];
  const browser = await chromium.launch({channel: 'chrome', headless: false});
  const context = await browser.newContext({...L.VIEWPORTS.desktop, locale: 'en-US'});
  await context.addInitScript(L.SSE_MONITOR);
  const page = await context.newPage();
  L.attachNetwork(page, net);

  try {
    rec.stage = 'enter';
    await page.goto(`${BASE}/?token=${seed}`, {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
    rec.stage = 'navigate';
    const resp = await page.goto(`${BASE}/c/${convId}`, {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
    rec.pageStatus = resp.status();

    // Give the SPA a generous, fixed settle: the point is to see the finished
    // DOM, so waiting too long costs nothing while waiting too little lies.
    rec.stage = 'settle';
    await page.waitForTimeout(SETTLE_MS);

    rec.stage = 'survey';
    rec.survey = await page.evaluate(SURVEY);
    rec.sse = await page.evaluate(() => ({...window.__sse}));
    await page.screenshot({path: path.join(OUT, `dom-${label}.png`), fullPage: false});
    rec.stage = 'done';
  } catch (error) {
    rec.error = L.redact(error.message).slice(0, 400);
    rec.failedAtStage = rec.stage;
    await page.screenshot({path: path.join(OUT, `dom-${label}-failure.png`)}).catch(() => {});
  }

  rec.network = net.filter((n) => !n.pathname.startsWith('/cdn/'));
  await context.close();
  await browser.close();
  fs.mkdirSync(OUT, {recursive: true});
  fs.writeFileSync(path.join(OUT, `dom-${label}.json`), JSON.stringify(rec, null, 2));
  console.log(JSON.stringify({stage: rec.stage, error: rec.error, pageStatus: rec.pageStatus,
    counts: rec.survey && rec.survey.counts, sse: rec.sse,
    bodyTextLen: rec.survey && rec.survey.bodyTextLen,
    messageAttrs: rec.survey && rec.survey.messageAttrs,
    candidates: rec.survey && rec.survey.candidateMessageSelectors}, null, 2));
})();
