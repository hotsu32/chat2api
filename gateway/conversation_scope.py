"""Which Seed a conversation belongs to.

A mirror account is shared: one upstream account can be bound to several Seeds,
so a conversation id is only meaningful together with the Seed that created it.
Both the explicit generation routes and the catch-all proxy need that answer,
and they must not disagree -- so it lives here rather than in either of them.

Deliberately import-light: ``gateway/backend.py`` registers a catch-all route at
import time, so importing it from ``gateway/f_conversation_gateway.py`` would
register that catch-all *before* the f/conversation route and silently route real
turns through the generic proxy (measured: the turn still answered 200, but none
of the route's own behaviour ran).  This module registers no routes.
"""

import json

import utils.globals as globals


def body_conversation_id(raw) -> str:
    """Conversation id from a request body, or '' when there is none to check.

    A first turn legitimately carries no id, or null: the id only appears once
    the upstream stream starts.  Those must not be treated as a failed lookup.
    """
    try:
        payload = json.loads(raw) if raw else None
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    conversation_id = payload.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) else ""


def conversation_owner(conversation_id: str) -> str:
    """Which Seed the mirror has recorded for a conversation, '' if nobody.

    Reads the same in-memory view the conversation detail route checks, so the
    two decisions cannot disagree.  A conversation the mirror has never seen
    returns '' and is *allowed*: refusing it would break every first turn and
    every conversation that predates the mirror's history.

    The scan is over ``seed_map``.  If the fleet grows enough for that to matter,
    the right fix is an indexed ``conv_id -> seed`` lookup on the conversations
    table in ``utils/store.py``, not a cache here.
    """
    if not isinstance(conversation_id, str):
        return ""
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return ""
    for seed, entry in globals.seed_map.items():
        if not isinstance(entry, dict):
            continue
        conversations = entry.get("conversations", [])
        if isinstance(conversations, str):
            conversations = [conversations]
        if not isinstance(conversations, (list, tuple, set)):
            continue
        if conversation_id in conversations:
            return seed
    return ""


def conversation_is_foreign(conversation_id: str, requester: str) -> bool:
    """Whether a conversation the mirror knows belongs to a different Seed."""
    owner = conversation_owner(conversation_id)
    return bool(owner) and owner != requester
