"""Progress restoration is conversation-scoped even on a shared upstream account.

Synthetic endpoint responses prove gateway isolation, not official task schemas.
"""
import pytest


@pytest.fixture
def shared_context(mock_upstream, seed_account, seed_user, make_access_token, monkeypatch):
    token = seed_account(make_access_token(account_id='synthetic-shared', plan_type='plus'))
    seed_user('owner-seed', token, conversations=['owned-conversation'])
    seed_user('other-seed', token, conversations=[])

    def respond(self):
        self._record()
        self._json(200, {'activity': 'synthetic private activity', 'conversation_id': 'owned-conversation'})

    monkeypatch.setattr(mock_upstream.RequestHandlerClass, 'do_GET', respond)
    return token


@pytest.mark.parametrize('prefix', ['backend-api', 'backend-alt'])
@pytest.mark.parametrize('suffix', ['stream_status', 'async-status', 'textdocs'])
@pytest.mark.parametrize('bootstrap_header', [False, True])
def test_other_seed_cannot_read_conversation_subpath(client, mock_upstream, shared_context, prefix, suffix, bootstrap_header):
    response = client.get(f'/{prefix}/conversation/owned-conversation/{suffix}',
                          cookies={'token': 'other-seed'},
                          headers={'Authorization': f'Bearer {shared_context}'} if bootstrap_header else {})
    assert response.status_code == 404
    assert 'private activity' not in response.text
    assert not mock_upstream.records, 'ownership must be checked before upstream authentication or requests'


@pytest.mark.parametrize('suffix', ['stream_status', 'async-status', 'textdocs'])
def test_owner_can_read_progress_subpath(client, mock_upstream, shared_context, suffix):
    path = f'/backend-api/conversation/owned-conversation/{suffix}'
    response = client.get(path, cookies={'token': 'owner-seed'})
    assert response.status_code == 200
    assert any(r['path'] == path for r in mock_upstream.records)


def test_conversation_init_is_not_a_private_conversation_id(client, mock_upstream, shared_context):
    response = client.get('/backend-api/conversation/init', cookies={'token': 'other-seed'})
    assert response.status_code == 200
    assert any(r['path'] == '/backend-api/conversation/init' for r in mock_upstream.records)


def test_bootstrap_header_does_not_expose_account_wide_tasks(client, mock_upstream, shared_context):
    response = client.get('/backend-api/tasks', cookies={'token': 'other-seed'},
                          headers={'Authorization': f'Bearer {shared_context}'})
    assert 'private activity' not in response.text
    assert not mock_upstream.records


def test_progress_stream_cannot_grant_additional_conversation_ownership(client, mock_upstream, shared_context, monkeypatch):
    from utils import globals, store
    def streamed(self):
        self._record()
        self._send(200, b'data: {"conversation_id":"unowned-conversation"}\n\ndata: [DONE]\n\n', 'text/event-stream')
    monkeypatch.setattr(mock_upstream.RequestHandlerClass, 'do_GET', streamed)
    response = client.get('/backend-api/conversation/owned-conversation/stream_status', cookies={'token': 'owner-seed'})
    assert response.status_code == 200
    assert 'unowned-conversation' not in globals.seed_map['owner-seed']['conversations']
    assert all(c['conv_id'] != 'unowned-conversation' for c in store.list_seed_conversations('owner-seed'))


@pytest.mark.parametrize('path', ['/backend-api/f/conversation', '/backend-api/conversation'])
def test_other_seed_cannot_continue_owned_conversation(client, mock_upstream, shared_context, path):
    response = client.post(path, cookies={'token': 'other-seed'}, json={
        'action': 'next', 'conversation_id': 'owned-conversation',
        'model': 'auto', 'messages': [{'id': 'synthetic-message',
            'author': {'role': 'user'},
            'content': {'content_type': 'text', 'parts': ['Synthetic isolation check']}}],
    })
    assert response.status_code == 404
    assert not mock_upstream.records, 'foreign continuation must fail before upstream requests'
