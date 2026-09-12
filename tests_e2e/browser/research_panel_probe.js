/*
 * Drives the real research panel in a real browser against a scripted mirror.
 *
 * The page under test is served over HTTP by the pytest harness, so the panel's
 * own <script>/<link> tags, its same-origin fetch, its timers and its CSS are
 * all exercised exactly as they are in production.  What is scripted is the
 * upstream of the *mirror* -- the JSON the progress endpoints return -- not the
 * panel's behaviour.
 *
 * Usage: node research_panel_probe.js <baseUrl> <scenario>
 * Prints one JSON object on stdout.  Exit code 42 means "no browser available".
 */
'use strict';

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    'playwright',
    'playwright-core',
    '/Users/Zhuanz/.codex/skills/playwright-skill/node_modules/playwright',
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    try { return require(candidate); } catch (_) { /* try the next one */ }
  }
  return null;
}

const playwright = loadPlaywright();
if (!playwright) {
  process.stdout.write(JSON.stringify({unavailable: true}));
  process.exit(42);
}

const BASE = process.argv[2];
const SCENARIO = process.argv[3] || 'sequence';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(page, fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await page.evaluate(fn);
    if (value) return value;
    if (Date.now() > deadline) throw new Error('timeout waiting for ' + label);
    await sleep(100);
  }
}

async function panelState(page) {
  return page.evaluate(() => {
    const root = document.getElementById('c2a-rp');
    if (!root || root.hidden) return null;
    const text = (key) => {
      const el = root.querySelector('[data-key="' + key + '"]');
      return el ? el.textContent : null;
    };
    return {
      state: root.dataset.state,
      finished: root.dataset.finished,
      headline: root.querySelector('.c2a-rp-state').textContent,
      action: root.querySelector('.c2a-rp-action').textContent,
      evidence: root.querySelector('.c2a-rp-evidence').textContent,
      sources: text('sources'),
      events: text('events'),
      elapsed: text('elapsed'),
      innerText: root.innerText,
    };
  });
}

async function run() {
  const browser = await playwright.chromium.launch({headless: true});
  const report = {scenario: SCENARIO, checks: {}, failures: []};
  const check = (name, ok, detail) => {
    report.checks[name] = {ok: !!ok, detail: detail === undefined ? null : detail};
    if (!ok) report.failures.push(name + (detail === undefined ? '' : ': ' + JSON.stringify(detail)));
  };
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const consoleErrors = [];
    page.on('pageerror', (e) => consoleErrors.push(String(e)));
    // The restore scenario is about a refresh on a conversation URL; loading
    // the bare page first would make its own /active poll part of the count.
    if (SCENARIO !== 'restore') {
      await page.goto(BASE + '/', {waitUntil: 'domcontentloaded'});
    }

    if (SCENARIO === 'idle') {
      await sleep(7000);
      const state = await panelState(page);
      check('idle_panel_hidden', state === null, state);
      const hits = await fetch(BASE + '/__stats').then((r) => r.json());
      check('idle_keeps_polling', hits.active >= 2, hits);
    } else if (SCENARIO === 'sequence') {
      // The panel starts before any research turn exists, then a turn runs and
      // ends.  Every state the panel must show is on this path.
      await waitFor(page, () => {
        const el = document.querySelector('#c2a-rp .c2a-rp-action');
        return el && el.textContent.indexOf('正在分析资料') >= 0;
      }, 12000, 'in-progress activity');
      const progress = await panelState(page);
      check('in_progress_headline', progress.headline === '研究正在进行', progress);
      check('in_progress_sources_pending', progress.sources === '等待来源', progress);
      check('in_progress_finished_flag', progress.finished === 'false', progress);

      await waitFor(page, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.dataset.finished === 'true';
      }, 15000, 'terminal state');
      const done = await panelState(page);
      check('terminal_headline', done.headline === '研究已完成', done.headline);
      check('terminal_sources_counted', done.sources === '2 个', done.sources);
      check('terminal_evidence', done.evidence.indexOf('web.run') >= 0, done.evidence);
      check('no_ratio_in_rendered_text', done.innerText.indexOf('%') < 0, done.innerText);

      const requestsAtFinish = (await fetch(BASE + '/__stats').then((r) => r.json())).total;
      await sleep(3200);
      const after = await panelState(page);
      const requestsLater = (await fetch(BASE + '/__stats').then((r) => r.json())).total;
      check('terminal_elapsed_frozen', after.elapsed === done.elapsed,
            {before: done.elapsed, after: after.elapsed});
      check('terminal_stops_polling', requestsLater === requestsAtFinish,
            {at: requestsAtFinish, later: requestsLater});
    } else if (SCENARIO === 'error' || SCENARIO === 'cancelled') {
      await waitFor(page, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.dataset.finished === 'true';
      }, 12000, 'terminal state');
      const state = await panelState(page);
      const expectedState = SCENARIO === 'error' ? 'failed' : SCENARIO;
      check('terminal_state_attr', state.state === expectedState, state.state);
      const expected = SCENARIO === 'error' ? '研究失败' : '研究已取消';
      check('terminal_headline', state.headline === expected, state.headline);
      const requestsAtFinish = (await fetch(BASE + '/__stats').then((r) => r.json())).total;
      await sleep(3000);
      const requestsLater = (await fetch(BASE + '/__stats').then((r) => r.json())).total;
      check('terminal_stops_polling', requestsLater === requestsAtFinish,
            {at: requestsAtFinish, later: requestsLater});
    } else if (SCENARIO === 'malformed') {
      // The first responses are not usable JSON at all; the panel must survive
      // them and still render the turn once the endpoint recovers.
      await waitFor(page, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.hidden === false;
      }, 20000, 'panel after malformed responses');
      const state = await panelState(page);
      check('survives_malformed_body', state !== null && state.headline.length > 0, state);
      check('no_page_error', consoleErrors.length === 0, consoleErrors);
    } else if (SCENARIO === 'unreported') {
      await waitFor(page, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.hidden === false;
      }, 12000, 'panel');
      const state = await panelState(page);
      check('sources_unreported_wording', state.sources === '等待来源', state.sources);
    } else if (SCENARIO === 'empty_sources') {
      await waitFor(page, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.dataset.finished === 'true';
      }, 12000, 'terminal');
      const state = await panelState(page);
      check('empty_sources_wording', state.sources === '暂无来源', state.sources);
    } else if (SCENARIO === 'restore') {
      // A refresh on /c/<id>: the panel must ask for that conversation's
      // retained projection and render its terminal state.
      const restored = await browser.newPage({viewport: {width: 1440, height: 900}});
      await restored.goto(BASE + '/c/conv-abc', {waitUntil: 'domcontentloaded'});
      await waitFor(restored, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.dataset.finished === 'true';
      }, 12000, 'restored terminal state');
      const state = await panelState(restored);
      check('restored_headline', state.headline === '研究已完成', state.headline);
      check('restored_sources', state.sources === '3 个', state.sources);
      const stats = await fetch(BASE + '/__stats').then((r) => r.json());
      check('restore_used_the_conversation_route', stats.projection >= 1, stats);
      check('restore_did_not_need_active', stats.active === 0, stats);
    } else if (SCENARIO === 'mobile' || SCENARIO === 'desktop') {
      const box = SCENARIO === 'mobile' ? {width: 390, height: 844} : {width: 1440, height: 900};
      const view = await browser.newPage({viewport: box});
      await view.goto(BASE + '/', {waitUntil: 'domcontentloaded'});
      await waitFor(view, () => {
        const root = document.getElementById('c2a-rp');
        return root && root.hidden === false;
      }, 12000, 'visible panel');
      const geom = await view.evaluate(() => {
        const root = document.getElementById('c2a-rp');
        const composer = document.getElementById('composer');
        const r = root.getBoundingClientRect();
        const c = composer.getBoundingClientRect();
        const close = root.querySelector('button');
        const cb = close.getBoundingClientRect();
        const overlaps = !(r.bottom <= c.top || r.top >= c.bottom ||
                           r.right <= c.left || r.left >= c.right);
        return {
          panel: {top: r.top, bottom: r.bottom, left: r.left, right: r.right},
          composer: {top: c.top, bottom: c.bottom},
          overlaps,
          within_viewport: r.top >= 0 && r.left >= 0 &&
            r.bottom <= window.innerHeight && r.right <= window.innerWidth,
          close_button: {w: cb.width, h: cb.height},
          scrollable: getComputedStyle(root).overflowY,
        };
      });
      check('panel_does_not_cover_composer', geom.overlaps === false, geom);
      check('panel_within_viewport', geom.within_viewport === true, geom);
      check('close_target_tappable', geom.close_button.w >= 44 && geom.close_button.h >= 44,
            geom.close_button);
      check('panel_scrolls_instead_of_overflowing', geom.scrollable === 'auto', geom.scrollable);
      // Operable: the dismiss control must actually dismiss.
      await view.click('#c2a-rp button');
      check('dismiss_removes_panel',
            (await view.evaluate(() => !!document.getElementById('c2a-rp'))) === false);
      report.geometry = geom;
    } else {
      throw new Error('unknown scenario ' + SCENARIO);
    }
    check('no_uncaught_page_error', consoleErrors.length === 0, consoleErrors);
  } catch (error) {
    report.failures.push('probe error: ' + String(error && error.message || error));
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report));
  process.exit(report.failures.length ? 1 : 0);
}

run();
