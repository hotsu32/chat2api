"""Research progress survives a refresh -- and only its owner can read it.

A Deep Research turn is long and the browser is allowed to reload, sleep or
reconnect.  These tests drive a real turn through the FastAPI app against the
loopback upstream, then ask the mirror what it retained.

The upstream shapes used here are synthetic.  They prove the gateway's
retention, isolation and terminal handling; they do NOT prove any official
research schema.  See tmp/research-protocol/OFFICIAL_INTERFACE_EVIDENCE.md for
what is and is not captured.
"""

import asyncio
import json

import pytest

import utils.globals as globals

ACCOUNT = 'acc-rp'
SEED = 'seed-rp-owner'
OTHER_SEED = 'seed-rp-other'
CONVERSATION = 'conv-1'
RESTORE = f'/backend-api/research-progress/{CONVERSATION}'


@pytest.fixture
def bound_account(monkeypatch, tmp_path, make_access_token, seed_user, seed_account):
    """One Seed bound to one already-verified Plus account, owning CONVERSATION.

    Same shape as tests_e2e/test_f_conversation_upstream.py: the page has been
    rendered, so frontend_sync holds this account's website context.
    """
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    access = make_access_token(account_id=ACCOUNT, plan_type='plus')
    (tmp_path / (ACCOUNT + '.json')).write_text(json.dumps(
        {'account': {'id': ACCOUNT}, 'sessionToken': 'website-' + ACCOUNT}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        session = {'user': {'id': 'u-' + account_id, 'name': 'Private owner'},
                   'account': {'id': account_id, 'planType': 'plus'},
                   'accessToken': access, 'sessionToken': 'PRIVATE-SESSION'}
        return {'html': '<html></html>', 'session': session,
                'cookies': dict(cookies, **{'cf_clearance': 'cf-value'})}

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    seed_account(access, plan_type='plus')
    seed_user(SEED, access, plan_type='plus', conversations=[CONVERSATION])
    seed_user(OTHER_SEED, access, plan_type='plus', conversations=[])
    asyncio.run(frontend.get_frontend_template(access, access, {}))
    from gateway.research_progress import store
    store.clear()
    yield access
    store.clear()
    frontend.invalidate_frontend_cache()


def _event(payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return f'data: {data}\n\n'.encode()


def _payload():
    return {
        'model': 'gpt-5-6',
        'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                      'content': {'content_type': 'text', 'parts': ['research this']}}],
        'conversation_id': CONVERSATION,
        'parent_message_id': 'client-created-root',
    }


def _turn(client, cookies):
    return client.post('/backend-api/f/conversation', cookies=cookies, json=_payload())


def test_owner_can_restore_progress_after_a_turn(client, mock_upstream, bound_account, monkeypatch):
    unknown = _event({'type': 'phase_the_gateway_does_not_know', 'count': 3})
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        unknown + mock_upstream.conversation_sse)

    assert _turn(client, {'token': SEED}).status_code == 200

    restored = client.get(RESTORE, cookies={'token': SEED})
    assert restored.status_code == 200
    snapshot = restored.json()
    assert snapshot['conversation_id'] == CONVERSATION
    assert snapshot['terminal'] is True
    assert snapshot['state'] == 'complete'
    texts = ' '.join(e['text'] for e in snapshot['events'])
    assert 'phase_the_gateway_does_not_know' in texts, \
        'an unrecognised phase must be retained, not dropped'
    from gateway.research_progress import structure_fingerprint
    fingerprint = structure_fingerprint({'type': 'phase_the_gateway_does_not_know', 'count': 3})
    assert snapshot['unknown'].get(fingerprint) == 1, \
        'the unrecognised shape must be inventoried by field names, not renamed'


def test_restore_survives_a_refresh_without_touching_upstream(client, mock_upstream, bound_account):
    assert _turn(client, {'token': SEED}).status_code == 200
    before = len(mock_upstream.records)

    first = client.get(RESTORE, cookies={'token': SEED})
    second = client.get(RESTORE, cookies={'token': SEED})
    assert first.status_code == second.status_code == 200
    assert first.json()['events_seen'] == second.json()['events_seen']
    assert len(mock_upstream.records) == before, \
        'restoring must be served from the mirror, never by re-asking the account'


def test_another_seed_on_the_same_account_cannot_read_progress(client, mock_upstream, bound_account):
    assert _turn(client, {'token': SEED}).status_code == 200
    before = len(mock_upstream.records)

    response = client.get(RESTORE, cookies={'token': OTHER_SEED})
    assert response.status_code == 404
    assert 'conversation' not in response.text.lower() or 'not found' in response.text.lower()
    assert len(mock_upstream.records) == before, \
        'ownership must be decided before any upstream request'


def test_anonymous_reader_cannot_read_progress(client, mock_upstream, bound_account):
    assert _turn(client, {'token': SEED}).status_code == 200
    before = len(mock_upstream.records)
    assert client.get(RESTORE).status_code == 404
    assert len(mock_upstream.records) == before


def test_restore_reports_unknown_conversation_as_absent_not_empty(client, mock_upstream, bound_account):
    assert _turn(client, {'token': SEED}).status_code == 200
    assert client.get('/backend-api/research-progress/never-streamed',
                      cookies={'token': SEED}).status_code == 404


def test_repeated_terminal_marker_reaches_the_browser_once(client, mock_upstream, bound_account, monkeypatch):
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        mock_upstream.conversation_sse + _event('[DONE]') + _event('[DONE]'))
    response = _turn(client, {'token': SEED})
    assert response.status_code == 200
    assert response.text.count('[DONE]') == 1


def test_progress_events_reach_the_browser_verbatim(client, mock_upstream, bound_account, monkeypatch):
    """Retention must not become rewriting: the browser still gets the bytes."""
    marker = _event({'type': 'phase_the_gateway_does_not_know', 'count': 7})
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        marker + mock_upstream.conversation_sse)
    response = _turn(client, {'token': SEED})
    assert response.status_code == 200
    assert json.dumps({'type': 'phase_the_gateway_does_not_know', 'count': 7}) in response.text


def test_a_turn_does_not_grant_another_seed_ownership_of_the_conversation(
        client, mock_upstream, bound_account, monkeypatch):
    """Progress for a conversation may only ever exist under the Seed that ran it."""
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        _event({'conversation_id': 'unowned-conversation'})
                        + mock_upstream.conversation_sse)
    assert _turn(client, {'token': SEED}).status_code == 200

    assert 'unowned-conversation' in globals.seed_map[SEED]['conversations']
    assert 'unowned-conversation' not in globals.seed_map[OTHER_SEED]['conversations']
    assert client.get('/backend-api/research-progress/unowned-conversation',
                      cookies={'token': OTHER_SEED}).status_code == 404


def test_a_second_seed_cannot_continue_the_owners_conversation(
        client, mock_upstream, bound_account, monkeypatch):
    """Two Seeds share one upstream account; a foreign continuation is refused.

    Refused at the route, before any upstream request, and the owner's retained
    progress is untouched -- so the mirror cannot be used to write into another
    user's conversation on the same account.
    """
    assert _turn(client, {'token': SEED}).status_code == 200
    owned = client.get(RESTORE, cookies={'token': SEED}).json()
    before = len(mock_upstream.records)

    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        _event({'type': 'phase_the_gateway_does_not_know'})
                        + mock_upstream.conversation_sse)
    assert _turn(client, {'token': OTHER_SEED}).status_code == 404
    assert len(mock_upstream.records) == before, \
        'a foreign continuation must fail before the upstream is contacted'

    after = client.get(RESTORE, cookies={'token': SEED}).json()
    assert after == owned, 'a foreign turn must not alter the owner record'


def test_upstream_silence_budget_is_unchanged_when_not_configured(
        client, mock_upstream, bound_account, monkeypatch):
    from gateway import f_conversation_gateway as gateway_module
    from utils.configs import chat_request_timeout

    seen = []
    real_client = gateway_module.Client

    def spy(*args, **kwargs):
        seen.append(kwargs.get('timeout'))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(gateway_module, 'Client', spy)
    monkeypatch.delenv('CHAT_RESEARCH_TIMEOUT', raising=False)
    assert _turn(client, {'token': SEED}).status_code == 200
    assert seen and all(value == chat_request_timeout for value in seen)


def test_an_unconfigured_research_turn_gets_the_larger_bounded_silence_budget(
        client, mock_upstream, bound_account, monkeypatch):
    """The real-risk correction, asserted where it actually takes effect.

    An unconfigured deployment used to hand a research turn the chat budget, so
    a normal turn that went quiet for minutes between steps was killed with no
    terminal event.  The gateway must now open the research turn's own upstream
    client with the bounded research default -- and the ordinary chat turn must
    keep exactly the budget it had.
    """
    from gateway import f_conversation_gateway as gateway_module
    from gateway.research_progress import DEFAULT_RESEARCH_TIMEOUT
    from utils.configs import chat_request_timeout

    seen = []
    real_client = gateway_module.Client

    def spy(*args, **kwargs):
        seen.append(kwargs.get('timeout'))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(gateway_module, 'Client', spy)
    monkeypatch.delenv('CHAT_RESEARCH_TIMEOUT', raising=False)
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    # The turn's own streaming client is widened; the sentinel preflight is a
    # short non-streaming exchange and keeps the default deadline on purpose.
    assert seen[0] == DEFAULT_RESEARCH_TIMEOUT, seen
    assert DEFAULT_RESEARCH_TIMEOUT > chat_request_timeout
    assert all(value in (chat_request_timeout, DEFAULT_RESEARCH_TIMEOUT) for value in seen), seen

    # Ordinary chat on the same account is untouched: same conversation, no
    # research hints, still the chat budget.
    seen.clear()
    assert _turn(client, {'token': SEED}).status_code == 200
    assert seen and all(value == chat_request_timeout for value in seen), seen


def test_research_turn_gets_the_configured_silence_budget(
        client, mock_upstream, bound_account, monkeypatch):
    from gateway import f_conversation_gateway as gateway_module
    from utils.configs import chat_request_timeout

    seen = []
    real_client = gateway_module.Client

    def spy(*args, **kwargs):
        seen.append(kwargs.get('timeout'))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(gateway_module, 'Client', spy)
    monkeypatch.setenv('CHAT_RESEARCH_TIMEOUT', str(chat_request_timeout * 4))
    response = client.post('/backend-api/f/conversation', cookies={'token': SEED}, json={
        'model': 'gpt-5-6',
        'system_hints': ['research'],
        'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                      'content': {'content_type': 'text', 'parts': ['research this']}}],
        'conversation_id': CONVERSATION,
        'parent_message_id': 'client-created-root',
    })
    assert response.status_code == 200
    # The turn's own upstream client is widened; the sentinel preflight is a
    # short non-streaming exchange and keeps the default deadline on purpose.
    assert seen[0] == chat_request_timeout * 4, \
        'the streamed conversation request must carry the research budget'
    assert all(value >= chat_request_timeout for value in seen)


# NOTE: a client disconnect is NOT testable through TestClient.  httpx's ASGI
# transport buffers the response until the app finishes -- measured, a stream
# that ran 11.35 s delivered all 202 events and was recorded as
# `outcome=complete` even though the test stopped reading after the first line.
# Disconnect handling is therefore covered where it can be observed:
#   * tests/test_research_progress.py::test_abandoning_a_heartbeat_wrapped_stream_closes_it
#     (closing the wrapper closes the source generator, so the release path runs)
#   * tests/test_m2_stream_cancel.py (a real socket server observes the teardown)
#   * the `phase=stream_release outcome=cancelled` log line.
# An end-to-end disconnect needs a socket-level client or the browser.


# ---------------------------------------------------------------------------
# The panel's read paths: a real-shaped research turn, projected live
# ---------------------------------------------------------------------------
# Field names below come from tests/fixtures/deep_research_plus_shapes.json (a
# redacted capture of one real Plus turn).  The *values* are synthetic; nothing
# here claims a real upstream run.

RESEARCH_MODEL = 'gpt-5-6-thinking'
RESEARCH_HINT = 'plugin:connector_openai_deep_research'
PROJECTION = f'/backend-api/research-progress/{CONVERSATION}/projection'


def _research_payload():
    """The request shape a real Plus Deep Research turn actually sends."""
    return {
        'model': RESEARCH_MODEL,
        'system_hints': [RESEARCH_HINT],
        'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                      'content': {'content_type': 'text', 'parts': ['research this']}}],
        'conversation_id': CONVERSATION,
        'parent_message_id': 'client-created-root',
    }


RESEARCH_REPORT = '最终研究报告：结论正文。'


def _research_stream(with_sources=True, with_report=False):
    """A research turn shaped after the capture, plus the observed terminal.

    ``with_report`` appends the observed answer frame: the assistant message
    that carries ``is_complete``, ``end_turn``, ``citations`` and a body.  It is
    off by default so the frame counts the other tests assert stay exact.
    """
    frames = [
        # deep_research_version is the observed marker that upstream is running
        # the research variant, and content_type=thoughts is an observed value.
        json.dumps({'c': 2, 'v': {'conversation_id': CONVERSATION, 'message': {
            'content': {'content_type': 'thoughts', 'parts': ['x']},
            'status': 'in_progress',
            'metadata': {'deep_research_version': 'observed-marker'}}}}),
        json.dumps({'conversation_id': CONVERSATION, 'type': 'message_marker',
                    'message_id': 'm1', 'event': 'search', 'marker': 'search'}),
        json.dumps({'c': 12, 'v': {'conversation_id': CONVERSATION, 'message': {
            'content': {'content_type': 'text', 'parts': ['x']},
            'recipient': 'web.run', 'status': 'in_progress',
            'metadata': {
                'content_references': ([{'type': 'sources', 'items': [
                    {'url': 'https://a.test/1'}, {'url': 'https://a.test/1'},
                    {'url': 'https://a.test/2'}]}] if with_sources else []),
                'citations': [], 'search_result_groups': []}}}}),
        # The answer frame the official page replaces with its own region; the
        # mirror shows the body instead.  ``chatgpt_sdk`` is the widget metadata
        # the capture records on this frame.
        json.dumps({'c': 13, 'v': {'conversation_id': CONVERSATION, 'message': {
            'author': {'role': 'assistant', 'name': None},
            'content': {'content_type': 'text', 'language': 'zh-CN', 'text': RESEARCH_REPORT},
            'end_turn': True, 'status': 'finished_successfully',
            'metadata': {'is_complete': True, 'finish_details': {'type': 'stop'},
                         'citations': [], 'content_references': [],
                         'chatgpt_sdk': {'resource_name': 'deep_research'}}}}})
        if with_report else None,
        json.dumps({'conversation_id': CONVERSATION, 'type': 'server_ste_metadata',
                    'metadata': {'tool_name': 'web.run', 'tool_invoked': True}}),
        json.dumps({'conversation_id': CONVERSATION, 'type': 'message_stream_complete'}),
        '[DONE]',
    ]
    frames = [frame for frame in frames if frame is not None]
    return ('data: ' + '\n\ndata: '.join(frames) + '\n\n').encode('utf-8')


def _research_turn(client, cookies, monkeypatch, mock_upstream, with_sources=True,
                   with_report=False):
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        _research_stream(with_sources, with_report))
    return client.post('/backend-api/f/conversation', cookies=cookies,
                       json=_research_payload())


def test_a_real_shaped_turn_projects_activity_sources_and_a_terminal(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200

    body = client.get(PROJECTION, cookies={'token': SEED}).json()
    assert body['research'] is True
    assert body['conversation_id'] == CONVERSATION
    assert body['state'] == 'complete' and body['terminal'] is True
    projection = body['projection']
    assert projection['finished'] is True
    assert projection['research_confirmed'] is True, 'deep_research_version was observed'
    assert projection['tool'] == 'web.run'
    assert projection['markers'] == ['search']
    assert projection['sources'] == 2, 'distinct evidenced sources, not frames'
    assert projection['sources_evidenced'] is True
    assert projection['urls_moderated'] == 0
    assert projection['action'] == '研究已完成'


def test_the_projection_route_never_returns_upstream_event_text(
        client, mock_upstream, bound_account, monkeypatch):
    """The panel path is a closed key set; the forensic path is separate."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    body = client.get(PROJECTION, cookies={'token': SEED}).json()
    assert set(body['projection']) == set(__import__(
        'gateway.research_progress', fromlist=['PROJECTION_KEYS']).PROJECTION_KEYS)
    rendered = json.dumps(body)
    assert 'events' not in body and 'a.test' not in rendered
    assert 'observed-marker' not in rendered, 'metadata values stay out of the panel'
    # The forensic endpoint still retains the stream itself.
    forensic = client.get(RESTORE, cookies={'token': SEED}).json()
    assert 'a.test' in json.dumps(forensic['events'])


def test_sources_are_unreported_rather_than_zero_when_upstream_listed_none(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_sources=False).status_code == 200
    projection = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    assert projection['sources'] == 0
    assert projection['sources_evidenced'] is True, \
        'upstream did list sources; it listed none'


def test_projection_restore_serves_a_refresh_without_touching_upstream(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    before = len(mock_upstream.records)
    first = client.get(PROJECTION, cookies={'token': SEED}).json()
    second = client.get(PROJECTION, cookies={'token': SEED}).json()
    assert first == second, 'a refresh must see exactly the retained state'
    assert len(mock_upstream.records) == before


def test_the_frozen_clock_survives_a_restore(
        client, mock_upstream, bound_account, monkeypatch):
    """A finished turn's elapsed time must not keep growing after a reload."""
    import time
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    first = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    time.sleep(0.05)
    second = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    assert first['elapsed_ms'] == second['elapsed_ms']
    assert first['finished_at'] == second['finished_at']


def test_projection_restore_is_owner_isolated_and_makes_no_upstream_call(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    before = len(mock_upstream.records)
    assert client.get(PROJECTION, cookies={'token': OTHER_SEED}).status_code == 404
    assert client.get(PROJECTION).status_code == 404
    assert client.get('/backend-api/research-progress/never-streamed/projection',
                      cookies={'token': SEED}).status_code == 404
    assert len(mock_upstream.records) == before


def test_a_non_research_turn_never_appears_in_the_panel(
        client, mock_upstream, bound_account):
    """Ordinary chat must cost the panel nothing and show it nothing."""
    assert _turn(client, {'token': SEED}).status_code == 200
    assert client.get('/backend-api/research-progress/active',
                      cookies={'token': SEED}).json() == {'research': False}
    assert client.get(PROJECTION, cookies={'token': SEED}).status_code == 404


def test_an_unrecognised_event_does_not_become_an_activity_claim(
        client, mock_upstream, bound_account, monkeypatch):
    """Unknown frames are counted, never labelled."""
    unknown = _event({'type': 'phase_the_gateway_does_not_know', 'count': 3})
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        unknown + _research_stream())
    response = client.post('/backend-api/f/conversation', cookies={'token': SEED},
                           json=_research_payload())
    assert response.status_code == 200
    body = client.get(PROJECTION, cookies={'token': SEED}).json()
    assert body['events_seen'] == 7, 'the unknown frame is still accounted for'
    for label in body['projection']['markers'] + [body['projection']['family']]:
        assert 'phase_the_gateway_does_not_know' not in label


def test_the_active_route_reports_which_conversation_the_panel_is_showing(
        client, mock_upstream, bound_account, monkeypatch):
    """Without the id a refresh cannot tell this turn from an older one."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    body = client.get('/backend-api/research-progress/active', cookies={'token': SEED}).json()
    assert body['conversation_id'] == CONVERSATION


# ---------------------------------------------------------------------------
# The answer the official region hides
# ---------------------------------------------------------------------------
# Live gap, 2026-09-13: a real Pro research turn streamed and completed, and the
# page still showed no report -- the official research region is keyed on a model
# identifier a normal browser cannot resolve.  These tests drive the same shape
# through the real routes and assert the mirror's own panel path carries the
# answer, bounded, owner-scoped, and restorable after a refresh.

PROJECTION = f'/backend-api/research-progress/{CONVERSATION}/projection'


def test_a_completed_turn_exposes_the_answer_body_the_official_region_hides(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    body = client.get(PROJECTION, cookies={'token': SEED}).json()
    projection = body['projection']
    assert projection['report'] == RESEARCH_REPORT
    assert projection['report_final'] is True
    assert projection['report_truncated'] is False
    assert body['state'] == 'complete' and projection['finished'] is True
    # The answer is the assistant's; the user's own question was echoed upstream
    # in the same stream and must not appear anywhere in the projection.
    rendered = json.dumps(body, ensure_ascii=False)
    assert 'research this' not in rendered
    assert 'observed-marker' not in rendered
    assert 'chatgpt_sdk' not in rendered
    assert 'a.test' not in rendered


def test_the_report_restores_after_a_refresh_without_touching_upstream(
        client, mock_upstream, bound_account, monkeypatch):
    """The refresh path is the one a real user hits: it must show the answer."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    before = len(mock_upstream.records)
    first = client.get(PROJECTION, cookies={'token': SEED}).json()
    second = client.get(PROJECTION, cookies={'token': SEED}).json()
    assert first == second
    assert first['projection']['report'] == RESEARCH_REPORT
    assert len(mock_upstream.records) == before, 'a restore must not re-run the turn'


def test_the_report_route_stays_owner_scoped(client, mock_upstream, bound_account, monkeypatch):
    """One Seed's answer must not be readable by another on the same account."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    assert client.get(PROJECTION, cookies={'token': OTHER_SEED}).status_code == 404
    assert client.get(PROJECTION).status_code == 404
    assert client.get('/backend-api/research-progress/active',
                      cookies={'token': OTHER_SEED}).json() == {'research': False}


# ---------------------------------------------------------------------------
# The panel follows the newest turn, not the newest research turn
# ---------------------------------------------------------------------------
# The active poll is what puts the panel on screen.  A chat turn that starts
# after a research turn used to leave the finished research panel on screen for
# the whole retention TTL, claiming a research turn was running over an
# unrelated chat.

ACTIVE = '/backend-api/research-progress/active'


def test_a_later_chat_turn_in_the_same_conversation_retires_the_active_panel(
        client, mock_upstream, bound_account, monkeypatch):
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json()['research'] is True

    # The same conversation, this time an ordinary chat turn.
    assert _turn(client, {'token': SEED}).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json() == {'research': False}


def test_a_later_chat_turn_in_another_conversation_retires_the_active_panel(
        client, mock_upstream, bound_account, monkeypatch):
    """The Seed's newest turn is what the panel is about, wherever it runs."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json()['research'] is True

    other = dict(_payload(), conversation_id='conv-2')
    assert client.post('/backend-api/f/conversation', cookies={'token': SEED},
                       json=other).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json() == {'research': False}


def test_the_retired_panel_does_not_take_the_report_with_it(
        client, mock_upstream, bound_account, monkeypatch):
    """Retiring the claim must not retire the record a refresh needs.

    The panel is put away because no research turn is *current*; the answer the
    conversation already produced stays restorable, which is the whole point of
    retaining it.
    """
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    assert _turn(client, {'token': SEED}).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json() == {'research': False}

    before = len(mock_upstream.records)
    restored = client.get(PROJECTION, cookies={'token': SEED})
    assert restored.status_code == 200
    assert restored.json()['projection']['report'] == RESEARCH_REPORT
    assert len(mock_upstream.records) == before, 'a restore must not re-run the turn'


def test_a_second_research_turn_puts_the_panel_back(
        client, mock_upstream, bound_account, monkeypatch):
    """Retiring on a chat turn must not mean "never again" for the Seed."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    assert _turn(client, {'token': SEED}).status_code == 200
    assert client.get(ACTIVE, cookies={'token': SEED}).json() == {'research': False}

    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_report=True).status_code == 200
    body = client.get(ACTIVE, cookies={'token': SEED}).json()
    assert body['research'] is True
    assert body['conversation_id'] == CONVERSATION
    assert body['projection']['report'] == RESEARCH_REPORT


def test_a_turn_with_no_answer_body_reports_none_rather_than_inventing_one(
        client, mock_upstream, bound_account, monkeypatch):
    """The mirror's honest fallback: a terminal state with an empty report.

    This is exactly the ``sources=0`` shape the live gap reported.  Nothing in
    the stream carried an answer, so the panel must say so instead of showing a
    body, a stage or a summary the upstream never sent.
    """
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream).status_code == 200
    projection = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    assert projection['report'] == ''
    assert projection['report_final'] is False
    assert projection['report_truncated'] is False
    assert projection['finished'] is True, 'the turn still reached its terminal'


def test_citations_are_counted_when_the_stream_evidenced_them(
        client, mock_upstream, bound_account, monkeypatch):
    """Sources stay a count, and the answer body is never a source carrier."""
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_sources=True, with_report=True).status_code == 200
    projection = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    assert projection['sources'] == 2
    assert projection['sources_evidenced'] is True
    assert projection['report'] == RESEARCH_REPORT, \
        'the report is the assistant body, never a citation list'


def test_zero_sources_are_reported_as_zero_when_upstream_listed_none(
        client, mock_upstream, bound_account, monkeypatch):
    """The live gap's own shape: an evidenced, empty source list.

    It must stay a zero -- an empty citation list is a fact, and the report is
    still shown next to it.
    """
    assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                          with_sources=False, with_report=True).status_code == 200
    projection = client.get(PROJECTION, cookies={'token': SEED}).json()['projection']
    assert projection['sources'] == 0
    assert projection['sources_evidenced'] is True
    assert projection['report'] == RESEARCH_REPORT


def test_the_phase_log_records_the_projection_in_counts_only(
        client, mock_upstream, bound_account, monkeypatch, caplog):
    """The one line a real deployment has to prove retention happened.

    It must be usable for diagnosis and useless for leaking: counts and a state
    name, no event text, no metadata value, no prompt, no credential.
    """
    import logging
    with caplog.at_level(logging.INFO):
        assert _research_turn(client, {'token': SEED}, monkeypatch,
                              mock_upstream).status_code == 200
    lines = [record.getMessage() for record in caplog.records
             if 'phase=research_progress' in record.getMessage()]
    assert len(lines) == 1, caplog.text[-2000:]
    line = lines[0]
    assert 'retained=yes' in line and 'state=complete' in line
    assert 'sources=2' in line and 'sources_reported=True' in line
    # Six frames were sent and six were retained: one per message frame plus the
    # stream_complete frame and the [DONE] terminal.
    assert 'retained_events=6' in line
    # No answer frame in this stream, so the measured report length is zero --
    # and it is a length, not the answer.
    assert 'report_chars=0' in line and 'report_final=False' in line
    for leak in (SEED, 'a.test', 'observed-marker', 'research this',
                 'private-session', 'PRIVATE-SESSION', 'cf-value'):
        assert leak not in line, leak


def test_the_phase_log_measures_a_real_report_without_printing_it(
        client, mock_upstream, bound_account, monkeypatch, caplog):
    """A real deployment needs to tell "no report" from "report never printed"."""
    import logging
    with caplog.at_level(logging.INFO):
        assert _research_turn(client, {'token': SEED}, monkeypatch, mock_upstream,
                              with_report=True).status_code == 200
    lines = [record.getMessage() for record in caplog.records
             if 'phase=research_progress' in record.getMessage()]
    assert len(lines) == 1, caplog.text[-2000:]
    line = lines[0]
    assert f'report_chars={len(RESEARCH_REPORT)}' in line, line
    assert 'report_final=True' in line, line
    assert 'retained_events=7' in line, line
    for leak in (RESEARCH_REPORT, '报告', 'a.test', 'research this', SEED):
        assert leak not in line, leak


def test_a_non_research_turn_logs_no_research_phase_line(
        client, mock_upstream, bound_account, caplog):
    """A normal chat must not pay for, or appear in, the research log."""
    import logging
    with caplog.at_level(logging.INFO):
        assert _turn(client, {'token': SEED}).status_code == 200
    assert 'phase=research_progress' not in caplog.text
