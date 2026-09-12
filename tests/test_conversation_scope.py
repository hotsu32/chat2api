"""Conversation ownership: whose conversation is this?

A mirror account is shared, so "the conversation id is in the request" is not
enough to authorise a turn.  The rule these tests pin down is deliberately
asymmetric:

* a conversation the mirror has **never recorded** is allowed -- refusing it
  would break every first turn, and every conversation that predates the
  mirror's history (the id is only learned once the upstream stream starts);
* a conversation the mirror **has recorded under another Seed** is refused
  before any upstream request.

These are gateway-isolation facts.  They say nothing about the upstream product.
"""

import json

import pytest

from gateway.conversation_scope import (
    body_conversation_id,
    conversation_is_foreign,
    conversation_owner,
)


@pytest.fixture
def seeds(monkeypatch):
    import utils.globals as globals
    monkeypatch.setattr(globals, 'seed_map', {
        'seed-owner': {'token': 'acct-a', 'conversations': ['conv-owned', 'conv-shared']},
        'seed-other': {'token': 'acct-a', 'conversations': []},
        'seed-broken': 'not-a-dict',
    }, raising=False)
    return globals.seed_map


@pytest.mark.parametrize('raw, expected', [
    (json.dumps({'conversation_id': 'conv-1'}).encode(), 'conv-1'),
    (json.dumps({'conversation_id': None}).encode(), ''),
    (json.dumps({'conversation_id': 42}).encode(), ''),
    (json.dumps({'other': 'field'}).encode(), ''),
    (json.dumps(['not', 'an', 'object']).encode(), ''),
    (b'not json at all', ''),
    (b'', ''),
    (None, ''),
])
def test_conversation_id_is_only_taken_when_it_is_actually_there(raw, expected):
    """An absent or unusable id means "nothing to check", never "denied"."""
    assert body_conversation_id(raw) == expected


def test_owner_is_found_across_the_whole_map(seeds):
    assert conversation_owner('conv-owned') == 'seed-owner'
    assert conversation_owner('conv-unknown') == ''
    assert conversation_owner('') == ''


def test_a_seed_entry_that_is_not_a_dict_does_not_break_the_lookup(seeds):
    assert conversation_owner('conv-owned') == 'seed-owner'


def test_malformed_conversation_lists_do_not_crash_or_match_substrings(monkeypatch):
    import utils.globals as globals
    monkeypatch.setattr(globals, 'seed_map', {
        'none': {'conversations': None},
        'string': {'conversations': 'conv-owned'},
    }, raising=False)
    assert conversation_owner('conv-owned') == 'string'
    assert conversation_owner('owned') == ''


def test_an_unrecorded_conversation_is_allowed(seeds):
    """The first turn carries an id the mirror has never seen; it must proceed."""
    assert conversation_is_foreign('conv-unknown', 'seed-other') is False
    assert conversation_is_foreign('', 'seed-other') is False


def test_the_owner_may_continue_their_own_conversation(seeds):
    assert conversation_is_foreign('conv-owned', 'seed-owner') is False


def test_another_seed_sharing_the_account_may_not(seeds):
    assert conversation_is_foreign('conv-owned', 'seed-other') is True


def test_an_anonymous_caller_may_not_continue_a_recorded_conversation(seeds):
    assert conversation_is_foreign('conv-owned', '') is True
