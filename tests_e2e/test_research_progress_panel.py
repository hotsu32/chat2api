"""The research panel's contract: honest, operable, and impossible to miss.

The panel is served as two static assets and driven by one server-side
projection endpoint.  These tests pin the properties that a future edit could
quietly break, in the order they matter:

* it must never imply progress the upstream never reported (no ratio, no stage
  list, no progressbar role);
* it must reach a readable terminal state and then stop polling;
* it must restore from the mirror after a refresh instead of re-running a turn;
* it must not cover the composer on a phone;
* it must not read credentials.

The behavioural half -- that the polling sequence, the freeze and the layout
actually hold in a browser -- is driven for real in
``tests_e2e/test_research_progress_browser.py``.
"""

import json

import utils.globals as globals


def _js(client):
    return client.get("/_chat-share/research-panel.js").text


def _css(client):
    return client.get("/_chat-share/research-panel.css").text


def test_active_progress_is_seed_isolated(client):
    from gateway.research_progress import store
    store.clear()
    globals.seed_map["research-seed"] = {
        "token": "account-a", "plan_type": "plus", "conversations": ["c1"]
    }
    globals.seed_map["other-seed"] = {
        "token": "account-a", "plan_type": "plus", "conversations": []
    }
    recorder = store.recorder("research-seed", "c1", research=True)
    recorder.record(b'data: {"type":"input_message"}\n\n')

    owned = client.get("/backend-api/research-progress/active",
                       cookies={"token": "research-seed"})
    other = client.get("/backend-api/research-progress/active",
                       cookies={"token": "other-seed"})
    assert owned.status_code == 200 and owned.json()["research"] is True
    assert other.status_code == 200 and other.json() == {"research": False}
    assert "events" not in owned.json()
    assert "text" not in json.dumps(owned.json())


def test_panel_assets_are_public_and_do_not_read_credentials(client):
    js = client.get("/_chat-share/research-panel.js")
    css = client.get("/_chat-share/research-panel.css")
    assert js.status_code == css.status_code == 200
    assert "document.cookie" not in js.text
    assert "localStorage" not in js.text
    assert "sessionStorage" not in js.text
    assert "innerHTML" not in js.text
    assert "research-progress/active" in js.text
    assert "progressbar" not in js.text.lower()
    assert "percent" not in js.text.lower()
    assert "％" not in js.text
    assert js.headers["cache-control"] == "no-cache, must-revalidate"
    assert css.headers["cache-control"] == "no-cache, must-revalidate"


def test_panel_keeps_polling_before_a_research_turn_starts(client):
    """The panel is loaded before users choose Deep Research.

    An initial ``research:false`` response must therefore remain an idle state,
    not permanently stop discovery of a turn started later on the same page.
    """
    js = _js(client)
    assert "else if(verdict==='idle')schedule(POLL_IDLE)" in js
    assert "POLL_IDLE=3000" in js


def test_terminal_state_stops_the_timer_instead_of_polling_forever(client):
    """A finished turn has nothing left to learn; polling it is pure cost."""
    js = _js(client)
    assert "else{stop();armResume()}" in js
    assert "return p.finished===true?'finished':'active'" in js


def test_a_new_turn_is_discovered_again_after_a_terminal_state(client):
    """Stopping must not mean "never again": the next turn has to be found."""
    js = _js(client)
    assert "armResume" in js
    assert "addEventListener('pointerdown',resume,{passive:true,once:true})" in js


def test_panel_restores_the_viewed_conversation_from_the_server_store(client):
    """Refresh recovery: read the conversation from the URL, ask the mirror."""
    js = _js(client)
    assert "/^\\/c\\/([A-Za-z0-9_-]{1,128})/.exec(location.pathname)" in js
    assert "'/backend-api/research-progress/'+encodeURIComponent(viewed)+'/projection'" in js


def test_panel_falls_back_to_the_active_route_when_the_conversation_is_unknown(client):
    js = _js(client)
    assert "if(r.status===404&&source!==ACTIVE){source=ACTIVE;failures=0;schedule(0);return}" in js


def test_panel_reports_terminal_states_separately_from_activity(client):
    """A cancelled or failed turn must be readable as such, not as "writing"."""
    js = _js(client)
    for phrase in ("研究已完成", "研究已取消", "研究失败", "研究正在进行"):
        assert phrase in js
    assert "最近活动：" in js


def test_panel_never_shows_a_ratio_or_a_step_number(client):
    """``%`` is a legal JS operator, so the rendered text is checked in the
    browser test; here only the constructs that could render one are banned."""
    js = _js(client)
    for forbidden in ("progressbar", "percent", "ETA", "第 ", "共 ", "duration_ratio"):
        assert forbidden not in js, f"{forbidden} would imply progress upstream never sent"


def test_panel_labels_sources_as_unreported_rather_than_zero(client):
    """Zero sources and "upstream never listed sources" are different claims."""
    js = _js(client)
    assert "p.sources_evidenced?'暂无来源':'等待来源'" in js
    assert "p.sources>0" in js


def test_mobile_panel_is_docked_away_from_the_composer(client):
    """The composer is at the bottom; a bottom-docked card would cover it.

    Docking to the top makes non-overlap geometric instead of a measurement
    that drifts with the next upstream layout change.
    """
    css = _css(client)
    assert "#c2a-rp{position:fixed;top:72px;right:20px;bottom:auto" in css
    assert "@media(max-width:720px){#c2a-rp{top:56px;right:12px;left:12px;bottom:auto" in css
    assert "bottom:20px" not in css, "a bottom-docked panel can cover the composer"
    assert "!important" not in css, "overriding upstream styles would fight the page"


def test_mobile_panel_stays_bounded_and_operable(client):
    css = _css(client)
    assert "max-height:40vh" in css and "max-height:min(52vh,420px)" in css
    assert "overflow:auto" in css
    assert "width:44px;height:44px" in css, "the dismiss target must stay tappable"
    assert "aria-label" in _js(client)


def test_panel_never_claims_official_parity(client):
    js = _js(client)
    assert "不构成官方进度" in js
