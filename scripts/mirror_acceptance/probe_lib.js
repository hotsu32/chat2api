// Shared browser-probe primitives for the M5 acceptance matrix.
//
// This file exists because the previous probe produced numbers that could not be
// trusted, in five specific ways. Each is fixed here, and each fix is named so a
// reviewer can check it rather than take it on faith:
//
//  1. IDENTITY. The old probe polled `[data-message-author-role="assistant"]`
//     .last(). When the app re-renders a turn list, "last" can jump to a
//     different node, so a length going 8 -> 0 -> 702 looked like "the DOM was
//     wiped" when it may simply have been a different message. We now pin the
//     conversation id and the assistant message id on first sight, and every
//     later sample re-finds *that* node by id. If the pinned node disappears we
//     record `node_detached` -- distinct from "the node is there and empty".
//
//  2. TERMINAL SETTLE. The old probe stopped ~2.5 s after the last change. Late
//     mounts (buttons, and post-terminal DOM commits) were therefore invisible
//     and got recorded as absent. We keep sampling for >= 5 s after the terminal
//     frame before concluding anything.
//
//  3. ACTIONABILITY. "visible and not disabled" is a guess about whether a
//     button works. We additionally run a real trial: hit-test the element's own
//     centre point and confirm the element under the cursor is the button (or a
//     descendant). A button covered by an overlay reports actionable=false here
//     and would have reported true before.
//
//  4. UNMEASURED != ZERO. If no selector in a control's list ever matches, the
//     result is `status: "unmeasured"` with the selectors tried, never a 0 ms or
//     a `false`. A missing selector is a gap in the probe, not a finding about
//     the product.
//
//  5. NO PAYLOAD. The SSE monitor counts frames and timestamps the terminal. It
//     never stores token text, headers, query strings, or bodies. DOM samples
//     carry a length and a timestamp, not the text.
//
//  6. SERVER-SIDE GROUND TRUTH. "The browser showed nothing" and "the answer was
//     never produced" are different failures with different owners, and the DOM
//     alone cannot tell them apart. The monitor therefore also measures the
//     conversation-history GET: how many messages came back and how long each
//     assistant message's text is. Lengths only, never the text. When the DOM is
//     empty and the history says 700 chars, the defect is in rendering; when
//     both are empty, it is upstream.

const SSE_MONITOR = () => {
  // Observe streams by cloning the response: the page's own copy is handed back
  // untouched, so the probe cannot change what it is measuring.
  window.__sse = {
    streams: 0, events: 0, dataFrames: 0, commentFrames: 0,
    firstFrame: null, done: null, terminalKind: null, started: null,
    errorFrames: 0, errorShapes: [], byPath: {},
  };
  // Server-side view of the same turn, for fix 6.
  window.__conv = {fetches: 0, last: null, history: []};

  // Classify an error frame without retaining it. Enum-ish fields (code, type)
  // are kept because they are what makes an error actionable; free-text detail
  // is reduced to its length, since it can echo upstream content.
  const errorShape = (body) => {
    try {
      const o = JSON.parse(body);
      const e = (o && (o.error || o.detail)) || o;
      const pick = (k) => {
        const v = e && e[k];
        return typeof v === 'string' && v.length <= 64 ? v : undefined;
      };
      return {
        keys: Object.keys(o || {}).slice(0, 12),
        code: pick('code'), type: pick('type'),
        // The observed error frames carry `error_code`/`error` at the top level
        // rather than under an `error` object, and those two short enum strings
        // are the whole diagnosis -- without them nine error frames is just a
        // number. Still length-capped, so a message cannot ride along.
        errorCode: (typeof o.error_code === 'string' && o.error_code.length <= 64)
          ? o.error_code : undefined,
        errorField: (typeof o.error === 'string' && o.error.length <= 64)
          ? o.error : (o.error === null ? null : typeof o.error),
        messageLength: typeof o.message === 'string' ? o.message.length : null,
        detailLength: typeof (e && e.message) === 'string' ? e.message.length : null,
      };
    } catch (_) {
      return {keys: ['<unparsed>'], bodyLength: body.length};
    }
  };

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const started = performance.now();
    const res = await origFetch.apply(this, args);
    const url = (typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url)) || '';
    let pathname = '<unparsed>';
    try { pathname = new URL(url, location.origin).pathname; } catch (_) {}
    const ct = (res.headers && res.headers.get) ? (res.headers.get('content-type') || '') : '';

    // Conversation history: measure the server's own answer length (fix 6).
    if (/^\/backend-api\/conversations?\/[0-9a-f-]{8,}$/i.test(pathname) && ct.includes('json')) {
      window.__conv.fetches += 1;
      res.clone().json().then((d) => {
        const mapping = (d && d.mapping) || {};
        const msgs = Object.values(mapping)
          .map((n) => n && n.message)
          .filter((m) => m && m.author && m.content);
        const lengthOf = (m) => (m.content.parts || [])
          .filter((p) => typeof p === 'string')
          .reduce((n, p) => n + p.length, 0);
        const assistant = msgs.filter((m) => m.author.role === 'assistant');
        const snap = {
          perfnow: performance.now(),
          status: res.status,
          messageCount: msgs.length,
          assistantCount: assistant.length,
          // Lengths only. This is the number that decides render-vs-upstream.
          assistantLengths: assistant.map(lengthOf),
          assistantStatuses: assistant.map((m) => (m.status || null)),
          assistantRecipients: assistant.map((m) => (m.recipient || null)),
          endTurn: assistant.map((m) => (m.end_turn === undefined ? null : m.end_turn)),
        };
        window.__conv.last = snap;
        window.__conv.history.push(snap);
      }).catch(() => {});
      return res;
    }

    if (!ct.includes('text/event-stream') || !res.body) return res;

    window.__sse.streams += 1;
    if (window.__sse.started === null) window.__sse.started = started;
    window.__sse.byPath[pathname] = (window.__sse.byPath[pathname] || 0) + 1;

    const observed = res.clone();
    (async () => {
      const reader = observed.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const {done, value} = await reader.read();
        if (done) {
          if (window.__sse.done === null) {
            window.__sse.done = performance.now();
            window.__sse.terminalKind = 'stream_closed';
          }
          break;
        }
        buf += dec.decode(value, {stream: true});
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const raw of lines) {
          const line = raw.replace(/\r$/, '');
          if (line.startsWith(':')) { window.__sse.commentFrames += 1; continue; }
          if (!line.startsWith('data:')) continue;
          window.__sse.dataFrames += 1;
          window.__sse.events += 1;
          if (window.__sse.firstFrame === null) window.__sse.firstFrame = performance.now();
          const body = line.slice(5).trim();
          // Shape checks only -- substring tests on the frame, never retention.
          if (body === '[DONE]') {
            window.__sse.done = performance.now();
            window.__sse.terminalKind = 'DONE';
          } else if (window.__sse.terminalKind !== 'DONE' && body.includes('message_stream_complete')) {
            window.__sse.done = performance.now();
            window.__sse.terminalKind = 'message_stream_complete';
          }
          if (body.includes('"error"') || body.includes('Error in message stream')) {
            window.__sse.errorFrames += 1;
            // Keep the shape of the first few errors. Six error frames with no
            // shape recorded is an unactionable number -- this is what turns it
            // into a diagnosis.
            if (window.__sse.errorShapes.length < 4) {
              window.__sse.errorShapes.push({perfnow: performance.now(), ...errorShape(body)});
            }
          }
        }
        if (buf.length > 8192) buf = '';   // never accumulate payload
      }
    })().catch(() => {});
    return res;
  };
};

// --- page-context helpers, injected as strings via page.evaluate -------------

// Pin the assistant message we are measuring. Returns the id of the last
// assistant message that is a *descendant of the active conversation turn list*,
// so a stale message from a previous turn cannot be picked up.
const PIN_ASSISTANT = () => {
  const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  if (!nodes.length) return null;
  const el = nodes[nodes.length - 1];
  const id = el.getAttribute('data-message-id')
          || el.getAttribute('data-testid')
          || null;
  return {
    messageId: id,
    index: nodes.length - 1,
    total: nodes.length,
    path: location.pathname,
    len: (el.innerText || '').trim().length,
    perfnow: performance.now(),
  };
};

// Sample the pinned node by id. Falls back to positional index only when the
// node carries no id at all, and says so, so a reader can discount that sample.
//
// `id_detached` is a first-class outcome, not an error. The app replaces the
// optimistic streaming node (a client-side `WEB:<uuid>` id) with the persisted
// one once the turn is saved, so the pinned id genuinely stops existing. The
// sample therefore also reports what the *tail* of the conversation looks like
// at that moment, so a detached pin can be distinguished from a wiped DOM:
// `tailLen > 0` with a detached pin means the node was replaced and the content
// is fine; `tailLen === 0` means the content really is gone.
const SAMPLE_PINNED = (pin) => {
  const all = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  let el = null, how = null;
  if (pin && pin.messageId) {
    el = document.querySelector(`[data-message-id="${CSS.escape(pin.messageId)}"]`);
    how = el ? 'by_id' : 'id_detached';
  }
  if (!el && pin && pin.messageId == null) {
    el = all[pin.index] || null;
    how = el ? 'by_index_no_id' : 'index_detached';
  }
  const tail = all[all.length - 1] || null;
  const tailId = tail ? tail.getAttribute('data-message-id') : null;
  return {
    perfnow: performance.now(),
    resolved: how,
    len: el ? (el.innerText || '').trim().length : null,
    // The tail is reported alongside the pin, never instead of it: a verdict
    // still comes from the pinned identity, but a detached pin is now
    // explainable rather than a dead end.
    tailLen: tail ? (tail.innerText || '').trim().length : null,
    tailIsPinned: !!(tailId && pin && tailId === pin.messageId),
    tailIdPresent: !!tailId,
    assistantCount: all.length,
    path: location.pathname,
    sse: {...window.__sse},
    conv: window.__conv ? window.__conv.last : null,
  };
};

// Actionability with a real hit test (fix 3). `trialClick` stays false by
// default: for a share control an actual click would publish, which the plan
// forbids, so the hit test is the strongest safe signal.
const PROBE_CONTROL = (selectors) => {
  for (const sel of selectors) {
    const els = Array.from(document.querySelectorAll(sel));
    if (!els.length) continue;
    const el = els[els.length - 1];
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
                 && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05;
    let hitOk = false, hitBlockedBy = null;
    if (visible) {
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const inView = cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight;
      if (inView) {
        const top = document.elementFromPoint(cx, cy);
        hitOk = !!top && (top === el || el.contains(top) || top.contains(el));
        if (!hitOk && top) hitBlockedBy = top.tagName.toLowerCase();
      } else {
        hitBlockedBy = 'offscreen';
      }
    }
    const enabledChecks = {
      notDisabledProp: !el.disabled,
      notAriaDisabled: el.getAttribute('aria-disabled') !== 'true',
      pointerEvents: cs.pointerEvents,
      pointerEventsOk: cs.pointerEvents !== 'none',
    };
    const enabled = enabledChecks.notDisabledProp && enabledChecks.notAriaDisabled
                 && enabledChecks.pointerEventsOk;
    return {
      status: 'matched',
      selector: sel,
      hits: els.length,
      visible,
      enabled,
      // Which specific condition failed. Without this an `enabled:false` is
      // unactionable -- "disabled by the app" and "pointer-events:none because
      // the bar only activates on hover" are completely different findings.
      enabledChecks,
      opacity: parseFloat(cs.opacity || '1'),
      hitTestPassed: hitOk,
      hitBlockedBy,
      actionable: visible && enabled && hitOk,
      testid: el.getAttribute('data-testid'),
      ariaLabel: el.getAttribute('aria-label'),
      perfnow: performance.now(),
    };
  }
  return {status: 'unmeasured', selectorsTried: selectors, perfnow: performance.now()};
};

// Turn-scoped variant of PROBE_CONTROL (fix 7).
//
// PROBE_CONTROL searches the whole document, which is right for a settled page
// with one turn but wrong during a live turn: the previous turn's action bar has
// been mounted for minutes and matches first. That produced a `copy` button
// whose mount time was 12 seconds BEFORE the terminal frame of the turn it was
// supposedly measuring -- an impossible number that would have read as an
// excellent 500 ms result. Here the search is confined to the container holding
// the pinned message, so a control can only be credited to its own turn.
const PROBE_TURN_CONTROL = ({selectors, messageId}) => {
  const msg = messageId
    ? document.querySelector(`[data-message-id="${CSS.escape(messageId)}"]`)
    : null;
  if (!msg) return {status: 'unmeasured', reason: 'pinned_message_absent',
                    selectorsTried: selectors, perfnow: performance.now()};

  // Walk up to the turn container: the action bar is a sibling of the message
  // body, not a descendant of it.
  let scope = msg.closest('article') || msg.parentElement;
  for (let i = 0; i < 4 && scope && scope.parentElement; i += 1) {
    if (scope.querySelector('button')) break;
    scope = scope.parentElement;
  }
  if (!scope) return {status: 'unmeasured', reason: 'no_turn_scope',
                      selectorsTried: selectors, perfnow: performance.now()};

  for (const sel of selectors) {
    const els = Array.from(scope.querySelectorAll(sel));
    if (!els.length) continue;
    const el = els[els.length - 1];
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
                 && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05;
    return {
      status: 'matched', selector: sel, hits: els.length, visible,
      scope: scope.tagName.toLowerCase(),
      pointerEvents: cs.pointerEvents,
      opacity: parseFloat(cs.opacity || '1'),
      testid: el.getAttribute('data-testid'),
      ariaLabel: el.getAttribute('aria-label'),
      perfnow: performance.now(),
    };
  }
  return {status: 'unmeasured', reason: 'no_selector_matched_in_turn',
          selectorsTried: selectors, scope: scope.tagName.toLowerCase(),
          perfnow: performance.now()};
};

// Selectors verified against the live DOM by discover_dom.js on 2026-09-12
// (evidence: dom-plus2.json). Three of the four control selectors the previous
// probe used do not exist in this frontend build, which is why its button
// timings could not be trusted:
//
//   * rate  -- it looked for `good-response-turn-action-button` /
//     `bad-response-turn-action-button` and `aria-label*="Good response"`.
//     The real control is a single `feedback-turn-action-button` labelled
//     "Rate response". Every one of those selectors misses, so rate was being
//     reported as absent no matter how fast it mounted.
//   * more  -- `more-actions-turn-action-button` does not exist; the button
//     carries `aria-label="More actions"` and no testid at all.
//   * share -- `share-chat-button` DOES exist, but it is the conversation
//     header button (y=8), present from first paint and unrelated to the turn.
//     The turn-level Share sits in the action bar (y=545) with no testid. A
//     probe matching the header button reports "share visible" essentially
//     instantly and always passes -- a false green, and the most dangerous of
//     the three.
//
// The turn action bar is therefore scoped to the last assistant turn wherever
// possible, so a header control can never satisfy a turn-level assertion.
const CONTROLS = {
  copy:  ['[data-testid="copy-turn-action-button"]', 'button[aria-label="Copy response" i]'],
  more:  ['button[aria-label="More actions" i]'],
  share: ['button[aria-label="Share" i]:not([data-testid="share-chat-button"])'],
  rate:  ['[data-testid="feedback-turn-action-button"]', 'button[aria-label="Rate response" i]'],
};

// Header controls, measured separately. They are legitimate product surface --
// they simply must never be counted as the turn action bar.
const HEADER_CONTROLS = {
  header_share: ['[data-testid="share-chat-button"]'],
  header_more:  ['[data-testid="conversation-options-button"]'],
};

const VIEWPORTS = {
  desktop: {viewport: {width: 1440, height: 1000}},
  mobile: {
    viewport: {width: 390, height: 844},
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 3,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 '
             + '(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  },
};

// Strip anything credential-shaped before a string reaches disk. Applied to
// error messages, which are the one place upstream text can leak through.
const redact = (s) => String(s == null ? '' : s)
  .replace(/eyJ[A-Za-z0-9_-]{8,}/g, '<redacted-jwt>')
  .replace(/token=[^&\s"']+/g, 'token=<redacted>')
  .replace(/(sess|__Secure[^=]*)=[^;\s]+/gi, '$1=<redacted>');

// Network rows keep pathname/method/status/duration only -- no query, no headers.
function attachNetwork(page, sink) {
  page.on('requestfinished', async (req) => {
    try {
      const r = await req.response();
      const t = req.timing();
      let p; try { p = new URL(req.url()).pathname; } catch (_) { p = '<unparsed>'; }
      sink.push({pathname: p, method: req.method(), status: r ? r.status() : null,
                 ms: t && t.responseEnd > 0 ? Math.round(t.responseEnd) : null});
    } catch (_) {}
  });
  page.on('requestfailed', (req) => {
    let p; try { p = new URL(req.url()).pathname; } catch (_) { p = '<unparsed>'; }
    sink.push({pathname: p, method: req.method(), status: 'failed', ms: null,
               failure: redact(req.failure() && req.failure().errorText).slice(0, 120)});
  });
}

module.exports = {
  SSE_MONITOR, PIN_ASSISTANT, SAMPLE_PINNED, PROBE_CONTROL, PROBE_TURN_CONTROL,
  CONTROLS, HEADER_CONTROLS, VIEWPORTS, redact, attachNetwork,
};
