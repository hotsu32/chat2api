"""M2: the gateway must not downgrade the client's stream encoding.

Measured symptom (tmp/agent-team/account-frontend/m2-streaming/gen-plus-shapes.json):
the browser receives 18 SSE frames over 4.3s, but the assistant DOM node is
*removed* for the whole streaming window and only repainted 2.2s after the
terminal — ``distinctLengths: 2``, i.e. one jump from nothing to the finished
reply.  The gateway is not buffering (see test_m2_incremental_delivery.py,
which proves deliveries are spread over time), so the defect is in *what* is
being streamed, not *when*.

The wire shapes say why.  Every assistant frame on the wire is a full-snapshot
message object::

    {"keys": ["conversation_id", "error", "error_code", "message"], "role": "assistant"}

There is not one ``{"p": ..., "o": "append", "v": ...}`` delta frame in the
stream.  The new official frontend posts to ``/backend-api/f/conversation`` and
renders from that delta encoding; given only snapshots it has no append path,
so it shows nothing until it reconciles at the terminal.

``f_conversation`` forces that: it overwrites whatever the client negotiated
with ``body["supported_encodings"] = []`` before forwarding to the legacy
``/backend-api/conversation``.  The client asked for the delta encoding and the
gateway silently downgraded it.

These tests pin the contract: the client's negotiated encodings must survive
the f/ -> legacy rewrite.
"""
import json

import pytest

from gateway.f_conversation_gateway import rewrite_f_conversation_body


def _client_body(**over):
    """A request body shaped like the one the official frontend posts."""
    body = {
        "action": "next",
        "messages": [{
            "id": "m1",
            "author": {"role": "user"},
            "create_time": 1.0,
            "content": {"content_type": "text", "parts": ["hi"]},
            "metadata": {"submission_mode": "primary",
                         "serialization_metadata": {"custom_symbol_offsets": []}},
        }],
        "parent_message_id": "client-created-root",
        "model": "gpt-5",
        "supported_encodings": ["v1"],
        "client_prepare_state": {"x": 1},
        "supports_buffering": True,
        "enable_message_followups": True,
        "force_parallel_switch": False,
        "local_function_names": [],
        "paragen_cot_summary_display_override": "allow",
    }
    body.update(over)
    return body


def test_client_requested_delta_encoding_is_forwarded_upstream():
    """The negotiated encoding must reach upstream unchanged.

    This is the M2 root cause.  Erasing it makes upstream emit full message
    snapshots instead of append deltas, and the frontend's renderer has no
    incremental path for snapshots — so the user sees nothing until the
    terminal event, which is exactly the reported symptom.
    """
    out = rewrite_f_conversation_body(_client_body())

    assert out["supported_encodings"] == ["v1"], (
        "gateway overwrote the client's supported_encodings; upstream will "
        "stream full snapshots and the browser cannot render incrementally"
    )


def test_absent_encoding_is_not_invented():
    """A client that negotiates nothing must not have a value forced on it."""
    body = _client_body()
    del body["supported_encodings"]

    out = rewrite_f_conversation_body(body)

    assert "supported_encodings" not in out or out["supported_encodings"] == [], (
        "gateway invented an encoding the client never asked for"
    )


def test_f_specific_fields_are_still_stripped():
    """The legacy endpoint rejects f/-only fields; they must still go.

    Guards against fixing the encoding by simply passing the body through.
    """
    out = rewrite_f_conversation_body(_client_body())

    for key in ("client_prepare_state", "supports_buffering",
                "enable_message_followups", "force_parallel_switch",
                "local_function_names", "paragen_cot_summary_display_override"):
        assert key not in out, f"{key} must be stripped before the legacy endpoint"
    msg = out["messages"][0]
    assert "create_time" not in msg
    assert "submission_mode" not in msg["metadata"]
    assert "serialization_metadata" not in msg["metadata"]


def test_client_created_root_parent_is_replaced_with_a_uuid():
    """Existing behaviour, pinned so the encoding fix cannot regress it."""
    out = rewrite_f_conversation_body(_client_body())

    assert out["parent_message_id"] != "client-created-root"
    assert len(out["parent_message_id"]) == 36


def test_rewrite_is_pure_and_does_not_mutate_the_caller_body():
    """The caller reads body['model'] for tier enforcement after rewriting."""
    body = _client_body()
    before = json.dumps(body, sort_keys=True)

    rewrite_f_conversation_body(body)

    assert json.dumps(body, sort_keys=True) == before, (
        "rewrite mutated the caller's body in place"
    )
