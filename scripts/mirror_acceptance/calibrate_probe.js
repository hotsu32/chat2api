// Probe self-calibration: prove the probe's own instruments before trusting them.
//
// This run costs ZERO generations. It opens an existing conversation, reloads
// it, and measures the same things a live turn would measure. Because no message
// is sent, the ground truth is known in advance:
//
//   * the pinned assistant node must resolve `by_id` on every sample -- if it
//     ever reports `id_detached` on a static page, the identity fix is broken
//     and no live measurement from this probe is worth anything;
//   * the DOM length must be stable and non-zero -- any "change" the probe
//     reports here is instrument noise, and tells us the live probe's
//     `dom_changes` count carries that much false positive;
//   * the controls must be present on a settled turn -- if a control reports
//     `unmeasured` here, the selector list is wrong, and its absence in a live
//     run means nothing about the product;
//   * no SSE stream should open at all -- a nonzero `streams` means reload is
//     re-generating, which would invalidate the "reload restores 702 chars"
//     reading from the earlier report.
//
// Usage: node calibrate_probe.js <seed> <convId> <label> [desktop|mobile]
const {chromium} = require('/Users/Zhuanz/.codex/skills/playwright-skill/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const L = require('./probe_lib.js');

const BASE = 'http://127.0.0.1:5025';
const OUT = process.env.M5_OUT || path.join(__dirname, '..', '..', 'tmp/agent-team/m5-acceptance/evidence');
const NAV_TIMEOUT = 45000;
const SAMPLE_MS = 120;
const OBSERVE_MS = 8000;   // > the 5 s settle window the live probe uses

(async () => {
  const [seed, convId, label, form = 'desktop'] = process.argv.slice(2);
  const rec = {
    kind: 'calibration', label, form, seed_alias: seed,
    conv_ref: convId ? convId.slice(0, 8) + '...' : null,
    checkedAt: new Date().toISOString(), stage: 'start', generationsUsed: 0,
  };
  const net = [];
  const browser = await chromium.launch({channel: 'chrome', headless: false});
  const context = await browser.newContext({...L.VIEWPORTS[form], locale: 'en-US'});
  await context.addInitScript(L.SSE_MONITOR);
  const page = await context.newPage();
  L.attachNetwork(page, net);

  try {
    // Two-step navigation, because /c/{id} authorises from the `token` cookie
    // and 404s without it. `/?token=` is the real Dashboard entry point and is
    // what sets that cookie, so this is the user's path, not a shortcut.
    rec.stage = 'enter';
    const entry = await page.goto(`${BASE}/?token=${seed}`,
      {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
    rec.entryStatus = entry.status();

    rec.stage = 'navigate';
    const resp = await page.goto(`${BASE}/c/${convId}`,
      {waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT});
    rec.pageStatus = resp.status();

    // Cookie hygiene is part of the M5 checklist ("Session/Refresh must not
    // leak"), and this is the one place the probe can see the flags directly.
    rec.cookies = (await context.cookies()).map((c) => ({
      name: c.name, httpOnly: c.httpOnly, secure: c.secure,
      sameSite: c.sameSite, path: c.path,
      valueLength: String(c.value || '').length,   // length only, never the value
    }));

    rec.stage = 'bootstrap';
    rec.bootstrap = await page.evaluate(() => {
      const node = document.getElementById('client-bootstrap');
      if (!node) return {present: false};
      const d = JSON.parse(node.textContent);
      return {
        present: true,
        planType: d.session && d.session.account && d.session.account.planType,
        authStatus: d.authStatus,
        // Leak check: the bootstrap blob is served to the browser, so anything
        // credential-shaped in it is a real exposure. Report shape, not value.
        hasAccessToken: !!(d.accessToken || (d.session && d.session.accessToken)),
        keys: Object.keys(d).sort(),
      };
    }).catch((e) => ({present: false, error: L.redact(e.message).slice(0, 200)}));

    rec.stage = 'pin';
    // The SPA hydrates and fetches the conversation before any message exists in
    // the DOM; on this runtime that takes ~20 s. Waiting on the selector (rather
    // than a fixed sleep) keeps the probe honest -- if it never appears we fail
    // loudly instead of sampling an empty page.
    await page.locator('[data-message-author-role="assistant"]').last()
      .waitFor({state: 'attached', timeout: NAV_TIMEOUT});
    const pin = await page.evaluate(L.PIN_ASSISTANT);
    rec.pin = pin;
    if (!pin) throw new Error('no assistant message rendered on an existing conversation');

    rec.stage = 'observe';
    const samples = [];
    const resolveModes = {};
    const t0 = Date.now();
    while (Date.now() - t0 < OBSERVE_MS) {
      const s = await page.evaluate(L.SAMPLE_PINNED, pin).catch(() => null);
      if (!s) break;
      resolveModes[s.resolved] = (resolveModes[s.resolved] || 0) + 1;
      samples.push({perfnow: Math.round(s.perfnow), len: s.len, resolved: s.resolved,
                    assistantCount: s.assistantCount});
      rec.sse = s.sse;
      await page.waitForTimeout(SAMPLE_MS);
    }

    rec.stage = 'controls';
    // Measure the action bar twice. The bar is hover-revealed: on a settled turn
    // every control sits at `pointer-events: none` until the pointer enters the
    // turn, so a no-hover reading reports all four as not-actionable and looks
    // like a product failure. Both readings are kept, because they answer
    // different questions -- "did it mount" (no-hover) and "can a user press it"
    // (hover). Conflating them is how a 500 ms button metric goes wrong in
    // either direction.
    const probeAll = async (set) => {
      const out = {};
      for (const [name, sels] of Object.entries(set)) {
        out[name] = await page.evaluate(L.PROBE_CONTROL, sels).catch(() => ({status: 'probe_error'}));
      }
      return out;
    };

    rec.controlsNoHover = await probeAll(L.CONTROLS);
    await page.locator('[data-message-author-role="assistant"]').last()
      .hover({timeout: 10000}).catch(() => {});
    await page.waitForTimeout(200);
    const controls = await probeAll(L.CONTROLS);
    rec.controls = controls;
    rec.hoverRequired = Object.fromEntries(Object.entries(controls).map(([name, c]) => [
      name,
      c.status === 'matched' && c.actionable === true
        && rec.controlsNoHover[name] && rec.controlsNoHover[name].actionable === false,
    ]));
    // Header controls are recorded alongside but never folded into the turn
    // verdict -- see the note on `share` in probe_lib.js.
    rec.headerControls = await probeAll(L.HEADER_CONTROLS);

    // --- verdicts: each one is a property of the *instrument*, not the product
    const lens = samples.map((s) => s.len);
    const distinct = [...new Set(lens)];
    rec.calibration = {
      sampleCount: samples.length,
      resolveModes,
      identity_stable: Object.keys(resolveModes).length === 1 && resolveModes.by_id === samples.length,
      dom_length_stable: distinct.length === 1,
      dom_length_values: distinct.slice(0, 8),
      instrument_false_change_count: Math.max(0, distinct.length - 1),
      body_nonempty: lens.every((l) => l != null && l > 0),
      sse_streams_on_reload: (rec.sse && rec.sse.streams) || 0,
      reload_is_zero_generation: ((rec.sse && rec.sse.streams) || 0) === 0,
      controls_all_matched: Object.values(controls).every((c) => c.status === 'matched'),
      controls_unmeasured: Object.entries(controls)
        .filter(([, c]) => c.status !== 'matched').map(([n]) => n),
      controls_all_actionable: Object.values(controls).every((c) => c.actionable === true),
    };
    rec.calibration.probe_trustworthy =
      rec.calibration.identity_stable &&
      rec.calibration.dom_length_stable &&
      rec.calibration.body_nonempty &&
      rec.calibration.reload_is_zero_generation;

    await page.screenshot({path: path.join(OUT, `calib-${label}.png`)});
    rec.stage = 'done';
  } catch (error) {
    rec.error = L.redact(error.message).slice(0, 400);
    rec.failedAtStage = rec.stage;
    await page.screenshot({path: path.join(OUT, `calib-${label}-failure.png`)}).catch(() => {});
  }

  rec.network = net;
  await context.close();
  await browser.close();
  fs.mkdirSync(OUT, {recursive: true});
  fs.writeFileSync(path.join(OUT, `calib-${label}.json`), JSON.stringify(rec, null, 2));
  console.log(JSON.stringify({label: rec.label, stage: rec.stage, error: rec.error,
    planType: rec.bootstrap && rec.bootstrap.planType,
    calibration: rec.calibration, controls: rec.controls}, null, 2));
})();
