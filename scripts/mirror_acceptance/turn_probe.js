// Measure one real streaming turn, with the credibility fixes from probe_lib.
//
// COSTS ONE GENERATION per run. The caller is responsible for the budget; this
// script records `generationsUsed: 1` so the matrix can account for it.
//
// What it answers, and how each answer avoids the earlier probe's mistakes:
//
//   M2 (incremental rendering): `dom_changes_before_terminal` counts DOM length
//   changes on the PINNED assistant node observed strictly before the SSE
//   terminal frame. Pinning by message id means a re-render cannot masquerade
//   as a content change, and cannot make real content look like it vanished.
//
//   M3 (buttons within 500 ms): two separate clocks, because conflating them is
//   how this metric goes wrong. `t_mounted` is when the control first exists and
//   is visible -- that is the local entry metric the plan's 500 ms applies to.
//   `t_actionable_after_hover` is a usability check, not a latency number: the
//   action bar is `pointer-events: none` until hover, so it measures when the
//   probe chose to hover, not when the product was ready.
//
//   Terminal handling: sampling continues for >= 5 s after the terminal frame,
//   so a control that mounts late is measured rather than reported missing.
//
// Usage: node turn_probe.js <seed> <label> [desktop|mobile] [--continue=<convId>]
//                           [--cancel-after-ms=N] [--prompt=<text>]
const {chromium} = require('/Users/Zhuanz/.codex/skills/playwright-skill/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const L = require('./probe_lib.js');

const BASE = 'http://127.0.0.1:5025';
const OUT = process.env.M5_OUT || path.join(__dirname, '..', '..', 'tmp/agent-team/m5-acceptance/evidence');
const DEFAULT_PROMPT =
  'List exactly 12 numbered short sentences about why the sky looks blue. '
  + 'One sentence per number, each under 15 words. No preamble.';
const POLL_MS = 50;
const NAV_TIMEOUT = 90000;
const REPLY_TIMEOUT = 180000;
const POST_TERMINAL_MS = 5500;   // > the 5 s the assignment requires
const STALL_LIMIT_MS = 45000;

function parseArgs(argv) {
  const out = {positional: []};
  for (const a of argv) {
    const m = /^--([^=]+)=(.*)$/.exec(a);
    if (m) out[m[1]] = m[2];
    else out.positional.push(a);
  }
  return out;
}

(async () => {
  const args = parseArgs(process.argv.slice(2));
  const [seed, label, form = 'desktop'] = args.positional;
  const continueConv = args['continue'] || null;
  const cancelAfterMs = args['cancel-after-ms'] ? parseInt(args['cancel-after-ms'], 10) : null;
  const prompt = args.prompt || DEFAULT_PROMPT;

  const rec = {
    kind: 'live_turn', label, form, seed_alias: seed,
    thread: continueConv ? 'continue' : 'new',
    cancelAfterMs, checkedAt: new Date().toISOString(),
    stage: 'start', generationsUsed: 1,
  };
  const net = [];
  const browser = await chromium.launch({channel: 'chrome', headless: false});
  const context = await browser.newContext({...L.VIEWPORTS[form], locale: 'en-US'});
  await context.addInitScript(L.SSE_MONITOR);
  const page = await context.newPage();
  L.attachNetwork(page, net);

  try {
    rec.stage = 'enter';
    const entry = await page.goto(`${BASE}/?token=${seed}`,
      {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
    rec.entryStatus = entry.status();

    if (continueConv) {
      rec.stage = 'open_thread';
      const r = await page.goto(`${BASE}/c/${continueConv}`,
        {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
      rec.threadStatus = r.status();
      // A continuation must start from a rendered history, otherwise it is just
      // a new chat wearing a conversation URL.
      await page.locator('[data-message-author-role="assistant"]').last()
        .waitFor({state: 'attached', timeout: NAV_TIMEOUT});
      rec.priorAssistantCount = await page.evaluate(
        () => document.querySelectorAll('[data-message-author-role="assistant"]').length);
    }

    rec.stage = 'bootstrap';
    rec.bootstrap = await page.evaluate(() => {
      const node = document.getElementById('client-bootstrap');
      if (!node) return {present: false};
      const d = JSON.parse(node.textContent);
      return {present: true, authStatus: d.authStatus,
              planType: d.session && d.session.account && d.session.account.planType};
    }).catch(() => ({present: false}));

    rec.stage = 'compose';
    const composer = page.locator('#prompt-textarea');
    await composer.waitFor({timeout: NAV_TIMEOUT});
    await composer.click();
    await page.keyboard.type(prompt);

    // Snapshot which assistant messages already exist, so the new turn can be
    // told apart from history rather than assumed to be "the last one".
    const priorIds = await page.evaluate(() => Array.from(
      document.querySelectorAll('[data-message-author-role="assistant"]'))
      .map((el) => el.getAttribute('data-message-id')).filter(Boolean));

    rec.stage = 'submit';
    await page.keyboard.press('Enter');
    rec.t_submit = Math.round(await page.evaluate(() => performance.now()));

    rec.stage = 'pin_new_assistant';
    // Wait for an assistant node that was NOT present before submitting.
    await page.waitForFunction((known) => {
      const ids = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'))
        .map((el) => el.getAttribute('data-message-id'));
      return ids.some((id) => id && !known.includes(id));
    }, priorIds, {timeout: REPLY_TIMEOUT});

    const pin = await page.evaluate((known) => {
      const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
      const fresh = nodes.filter((el) => {
        const id = el.getAttribute('data-message-id');
        return id && !known.includes(id);
      });
      const el = fresh[fresh.length - 1];
      return {messageId: el.getAttribute('data-message-id'), index: nodes.indexOf(el),
              total: nodes.length, path: location.pathname,
              len: (el.innerText || '').trim().length, perfnow: performance.now()};
    }, priorIds);
    rec.pin = {...pin, messageId: '<pinned>'};   // id is an upstream id; keep it out of evidence
    rec.t_first_assistant_node = Math.round(pin.perfnow);

    rec.stage = 'stream';
    const samples = [];
    const mounted = {};        // control -> perfnow of first visible mount
    const resolveModes = {};
    let len = pin.len, lastChange = pin.perfnow, changes = 0;
    let cancelled = false, terminalAt = null;
    // Track the pinned node across the app's optimistic->persisted swap. The
    // streaming node carries a client-side `WEB:<uuid>` id that ceases to exist
    // once the turn is saved; treating that as "content vanished" is exactly the
    // false negative the earlier probe produced. When the pin detaches and a
    // fresh tail node with a real id has taken its place, we re-pin to it and
    // record the handover -- the measurement continues on the same turn.
    let activePin = {...pin};
    const repins = [];
    const deadline = Date.now() + REPLY_TIMEOUT;

    while (Date.now() < deadline) {
      const snap = await page.evaluate(L.SAMPLE_PINNED, activePin).catch(() => null);
      if (!snap) break;

      if (snap.resolved === 'id_detached' && snap.tailIdPresent && !snap.tailIsPinned
          && snap.assistantCount >= pin.total) {
        const next = await page.evaluate(() => {
          const all = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
          const el = all[all.length - 1];
          if (!el) return null;
          return {messageId: el.getAttribute('data-message-id'), index: all.length - 1,
                  total: all.length, len: (el.innerText || '').trim().length,
                  perfnow: performance.now()};
        });
        if (next && next.messageId) {
          repins.push({at: Math.round(next.perfnow), lenBefore: len, lenAfter: next.len,
                       sameId: false});
          activePin = {...activePin, messageId: next.messageId, index: next.index};
          continue;   // re-sample against the new pin before drawing conclusions
        }
      }

      resolveModes[snap.resolved] = (resolveModes[snap.resolved] || 0) + 1;

      if (snap.len !== len) {
        samples.push({perfnow: Math.round(snap.perfnow), len: snap.len,
                      resolved: snap.resolved, tailLen: snap.tailLen,
                      before_terminal: snap.sse.done === null});
        len = snap.len;
        lastChange = snap.perfnow;
        changes += 1;
      }
      rec.sse = snap.sse;
      rec.conv = snap.conv;
      if (terminalAt === null && snap.sse.done !== null) terminalAt = snap.sse.done;

      // Mount timing, measured WITHOUT hover: this is the M3 entry metric.
      //
      // Scoped to the turn being measured. An unscoped document-wide query
      // matches the action bar of a PREVIOUS turn, which has been mounted for
      // minutes -- that yields a negative `ms_after_terminal` and would read as
      // a spectacularly fast button. Only controls inside the pinned turn's
      // article count, and only after the pinned node exists.
      for (const [name, sels] of Object.entries(L.CONTROLS)) {
        if (mounted[name] !== undefined) continue;
        const state = await page.evaluate(L.PROBE_TURN_CONTROL,
          {selectors: sels, messageId: activePin.messageId}).catch(() => null);
        if (state && state.status === 'matched' && state.visible) {
          mounted[name] = {perfnow: state.perfnow, selector: state.selector,
                           testid: state.testid, ariaLabel: state.ariaLabel,
                           scope: state.scope};
        }
      }

      // Optional cancel injection: press the stop button mid-stream.
      if (cancelAfterMs && !cancelled && snap.perfnow - pin.perfnow > cancelAfterMs) {
        const stop = page.locator('[data-testid="stop-button"], button[aria-label*="Stop" i]').last();
        rec.cancel = {attemptedAt: Math.round(snap.perfnow)};
        rec.cancel.buttonFound = await stop.count() > 0;
        if (rec.cancel.buttonFound) {
          await stop.click({timeout: 5000}).catch((e) => {
            rec.cancel.clickError = L.redact(e.message).slice(0, 160);
          });
          rec.cancel.lenAtCancel = snap.len;
        }
        cancelled = true;
      }

      // Keep sampling past the terminal so late mounts are seen (fix 2).
      if (terminalAt !== null && snap.perfnow - terminalAt > POST_TERMINAL_MS) break;
      if (snap.perfnow - lastChange > STALL_LIMIT_MS) {
        rec.stalled = true;
        break;
      }
      await page.waitForTimeout(POLL_MS);
    }

    rec.stage = 'settle';
    const finalSse = await page.evaluate(() => ({...window.__sse}));
    rec.sse = finalSse;
    if (terminalAt === null && finalSse.done !== null) terminalAt = finalSse.done;

    // Post-hover actionability, recorded separately and explicitly NOT used as
    // the 500 ms metric. Hover the pinned turn, not "the last message", so the
    // reading belongs to the turn being measured.
    await page.locator(`[data-message-id="${activePin.messageId}"]`).first()
      .hover({timeout: 10000}).catch(() => {});
    await page.waitForTimeout(200);
    const afterHover = {};
    for (const [name, sels] of Object.entries(L.CONTROLS)) {
      afterHover[name] = await page.evaluate(L.PROBE_CONTROL, sels).catch(() => ({status: 'probe_error'}));
    }

    rec.controls = {};
    for (const name of Object.keys(L.CONTROLS)) {
      const m = mounted[name];
      const hov = afterHover[name] || {};
      rec.controls[name] = m ? {
        status: 'measured',
        selector: m.selector, testid: m.testid, ariaLabel: m.ariaLabel,
        scope: m.scope,
        t_mounted: Math.round(m.perfnow),
        // The plan's metric: mount latency after the terminal frame. A negative
        // value here would mean the control was matched from a different turn,
        // so it is flagged rather than reported as a fast result.
        ms_after_terminal: terminalAt === null ? null : Math.round(m.perfnow - terminalAt),
        ms_after_last_dom_change: Math.round(m.perfnow - lastChange),
        suspect_pre_terminal_mount: terminalAt !== null && m.perfnow < terminalAt,
        actionable_after_hover: hov.actionable === true,
        hover_hit_blocked_by: hov.hitBlockedBy || null,
      } : {
        // Never a zero and never a false: an unseen control is a gap.
        status: 'unmeasured',
        selectorsTried: L.CONTROLS[name],
        note: 'never matched+visible during the sampling window',
        actionable_after_hover: hov.actionable === true,
      };
    }

    const finalLen = await page.evaluate((p) => {
      const el = document.querySelector(`[data-message-id="${CSS.escape(p.messageId)}"]`);
      const all = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
      const tail = all[all.length - 1] || null;
      const txt = el ? (el.innerText || '').trim() : '';
      const tailTxt = tail ? (tail.innerText || '').trim() : '';
      return {len: txt.length,
              numbered: txt.split('\n').filter((l) => /^\s*\d+[.)]/.test(l)).length,
              detached: !el,
              tailLen: tailTxt.length,
              tailNumbered: tailTxt.split('\n').filter((l) => /^\s*\d+[.)]/.test(l)).length};
    }, activePin);

    rec.assistant = {finalLength: finalLen.len, numberedLines: finalLen.numbered,
                     pinnedNodeDetached: finalLen.detached,
                     tailLength: finalLen.tailLen, tailNumberedLines: finalLen.tailNumbered};
    rec.repins = repins;
    rec.dom_samples = samples;
    rec.dom_resolve_modes = resolveModes;
    rec.dom_change_count = changes;
    // The M2 acceptance number. Only changes that grew visible text and landed
    // strictly before the terminal frame count.
    rec.dom_changes_before_terminal = samples.filter((s) => s.before_terminal && s.len > 0).length;
    rec.t_first_body = (samples.find((s) => s.len > 0) || {}).perfnow || null;
    rec.t_last_body = Math.round(lastChange);
    rec.t_terminal = terminalAt === null ? null : Math.round(terminalAt);
    rec.terminal_verified = terminalAt !== null;
    // A re-pin is a legitimate identity handover, not instability; an
    // `id_detached` sample that was NOT resolved by a re-pin is.
    rec.identity_stable = Object.keys(resolveModes).every((k) => k === 'by_id');
    rec.identity_handovers = repins.length;
    rec.conversationPath = await page.evaluate(() => location.pathname);

    // Server-side ground truth: did the answer exist at all? This is what
    // separates "the mirror failed to render" from "upstream produced nothing",
    // and neither the DOM nor the frame count can answer it alone.
    rec.conv = await page.evaluate(() => (window.__conv ? window.__conv.last : null));
    const serverLen = rec.conv && rec.conv.assistantLengths
      ? Math.max(0, ...rec.conv.assistantLengths) : null;
    rec.server_answer_length = serverLen;
    rec.render_gap = (serverLen !== null && serverLen > 0
                      && (rec.assistant.tailLength || 0) === 0);

    await page.screenshot({path: path.join(OUT, `turn-${label}.png`)});
    rec.stage = 'done';
  } catch (error) {
    rec.error = L.redact(error.message).slice(0, 400);
    rec.failedAtStage = rec.stage;
    await page.screenshot({path: path.join(OUT, `turn-${label}-failure.png`)}).catch(() => {});
  }

  rec.network = net.filter((n) => !n.pathname.startsWith('/cdn/'));
  await context.close();
  await browser.close();
  fs.mkdirSync(OUT, {recursive: true});
  fs.writeFileSync(path.join(OUT, `turn-${label}.json`), JSON.stringify(rec, null, 2));
  console.log(JSON.stringify({
    label, stage: rec.stage, error: rec.error,
    planType: rec.bootstrap && rec.bootstrap.planType,
    thread: rec.thread,
    finalLength: rec.assistant && rec.assistant.finalLength,
    tailLength: rec.assistant && rec.assistant.tailLength,
    server_answer_length: rec.server_answer_length,
    render_gap: rec.render_gap,
    dom_changes_before_terminal: rec.dom_changes_before_terminal,
    identity_stable: rec.identity_stable, identity_handovers: rec.identity_handovers,
    terminal: rec.terminal_verified, t_terminal: rec.t_terminal,
    sse: rec.sse, conv: rec.conv, controls: rec.controls, cancel: rec.cancel,
  }, null, 2));
})();
