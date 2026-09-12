"""SSE event-boundary parser unit tests.

These tests fail until gateway.sse_parser is created and the f/conversation
_filter_gen uses it.  They cover arbitrary chunk splits, sticky-packet
(multiple events per chunk), CRLF/CR/LF line endings, UTF-8 multi-byte
boundaries, comments, incomplete EOF, and both the sync and async variants.
"""
import asyncio

import pytest

from gateway.sse_parser import extract_data_json, iter_sse_events, iter_sse_events_async


# ---------------------------------------------------------------------------
# Sync iterator
# ---------------------------------------------------------------------------

def _events(chunks):
    return list(iter_sse_events(iter(chunks)))


def test_single_event_single_chunk():
    assert _events([b"data: hello\n\n"]) == [b"data: hello\n\n"]


def test_single_event_split_two_chunks():
    assert _events([b"data: hel", b"lo\n\n"]) == [b"data: hello\n\n"]


def test_single_event_split_at_first_newline():
    assert _events([b"data: hello\n", b"\n"]) == [b"data: hello\n\n"]


def test_two_events_one_chunk():
    result = _events([b"data: first\n\ndata: second\n\n"])
    assert result == [b"data: first\n\n", b"data: second\n\n"]


def test_three_events_mixed_chunks():
    result = _events([b"data: a\n\ndat", b"a: b\n\ndata: c\n\n"])
    assert result == [b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"]


def test_crlf_line_endings():
    assert _events([b"data: hello\r\n\r\n"]) == [b"data: hello\r\n\r\n"]


def test_cr_only_line_endings():
    assert _events([b"data: hello\r\r"]) == [b"data: hello\r\r"]


def test_comment_line_is_its_own_event():
    assert _events([b": keepalive\n\n"]) == [b": keepalive\n\n"]


def test_multiline_data_field():
    assert _events([b"data: line1\ndata: line2\n\n"]) == [b"data: line1\ndata: line2\n\n"]


def test_utf8_multibyte_split_at_byte_boundary():
    # "你" encodes to 3 bytes (e4 bd a0); split after the first byte.
    raw = "data: 你好\n\n".encode("utf-8")
    result = _events([raw[:6], raw[6:]])
    assert b"".join(result) == raw


def test_incomplete_event_at_eof_is_still_yielded():
    # Stream ends without terminal blank line — partial event must come out.
    assert _events([b"data: incomplete"]) == [b"data: incomplete"]


def test_done_event_passes_through():
    assert _events([b"data: [DONE]\n\n"]) == [b"data: [DONE]\n\n"]


def test_resume_token_event_isolated():
    import json
    payload = json.dumps({"type": "resume_conversation_token", "v": "tok123"})
    raw = (f"data: {payload}\n\n").encode()
    assert _events([raw]) == [raw]


def test_empty_chunks_ignored():
    assert _events([b"", b"data: hi\n\n", b""]) == [b"data: hi\n\n"]


# ---------------------------------------------------------------------------
# Async iterator
# ---------------------------------------------------------------------------

async def _async_events(chunks):
    async def _gen():
        for c in chunks:
            yield c

    result = []
    async for ev in iter_sse_events_async(_gen()):
        result.append(ev)
    return result


def test_async_single_event():
    assert asyncio.run(_async_events([b"data: hello\n\n"])) == [b"data: hello\n\n"]


def test_async_split_event():
    assert asyncio.run(_async_events([b"data: hel", b"lo\n\n"])) == [b"data: hello\n\n"]


def test_async_two_events_one_chunk():
    result = asyncio.run(_async_events([b"data: first\n\ndata: second\n\n"]))
    assert result == [b"data: first\n\n", b"data: second\n\n"]


def test_async_utf8_split():
    raw = "data: 你好\n\n".encode("utf-8")
    result = asyncio.run(_async_events([raw[:6], raw[6:]]))
    assert b"".join(result) == raw


def test_async_incomplete_eof():
    result = asyncio.run(_async_events([b"data: partial"]))
    assert result == [b"data: partial"]


# ---------------------------------------------------------------------------
# Regression: mixed line-ending terminators (the concrete repro from the issue)
# ---------------------------------------------------------------------------

def test_mixed_lf_then_crlf_two_events():
    """list(iter_sse_events([b'data: one\\n\\ndata: two\\r\\n\\r\\n'])) must give 2 items."""
    result = _events([b"data: one\n\ndata: two\r\n\r\n"])
    assert result == [b"data: one\n\n", b"data: two\r\n\r\n"]


def test_mixed_crlf_then_lf_two_events():
    result = _events([b"data: first\r\n\r\ndata: second\n\n"])
    assert result == [b"data: first\r\n\r\n", b"data: second\n\n"]


def test_three_events_all_mixed_endings():
    result = _events([b"data: a\n\ndata: b\r\n\r\ndata: c\r\r"])
    assert result == [b"data: a\n\n", b"data: b\r\n\r\n", b"data: c\r\r"]


# ---------------------------------------------------------------------------
# extract_data_json: extracts from plain and prefix-bearing events
# ---------------------------------------------------------------------------

def test_extract_data_json_plain_data():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b'data: {"type": "message"}\n\n') == {"type": "message"}


def test_extract_data_json_with_event_prefix():
    """event: delta\\ndata: {...} — a single SSE event with two fields."""
    from gateway.sse_parser import extract_data_json
    event = b"event: delta\ndata: {\"type\": \"message\"}\n\n"
    assert extract_data_json(event) == {"type": "message"}


def test_extract_data_json_with_id_prefix():
    from gateway.sse_parser import extract_data_json
    event = b'id: 42\ndata: {"v": {"conversation_id": "cid-1"}}\n\n'
    assert extract_data_json(event) == {"v": {"conversation_id": "cid-1"}}


def test_extract_data_json_done_returns_none():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b"data: [DONE]\n\n") is None


def test_extract_data_json_comment_returns_none():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b": keepalive\n\n") is None


def test_extract_data_json_multiline_data_joined_with_lf():
    """SSE joins ALL data values with LF and parses the result once.

    The old contract ("use the first JSON line") was wrong: joining
    `{"type": "message"}` with `continuation` yields invalid JSON, so the
    correct answer is None — not the first line's object.
    """
    from gateway.sse_parser import extract_data_json
    event = b'data: {"type": "message"}\ndata: continuation\n\n'
    assert extract_data_json(event) is None


def test_extract_data_json_json_object_split_across_data_lines():
    """A JSON object split across data fields reassembles into one payload."""
    from gateway.sse_parser import extract_data_json
    event = b'data: {\ndata: "conversation_id":"synthetic"}\n\n'
    assert extract_data_json(event) == {"conversation_id": "synthetic"}


def test_extract_data_json_strips_exactly_one_space_after_colon():
    """Only one optional space is removed; further spaces belong to the value.

    The retained space is observable inside a string value.  It must NOT make
    the event unparseable: leading whitespace around a JSON object is
    insignificant, and rejecting it drops real native events.
    """
    from gateway.sse_parser import extract_data_json
    # The second space is part of the value, so it survives inside the string.
    assert extract_data_json(b'data:  {"a": " x"}\n\n') == {"a": " x"}
    # ...and the event is still parsed rather than discarded.
    assert extract_data_json(b'data:  {"a": 1}\n\n') == {"a": 1}
    # No space at all is equally valid.
    assert extract_data_json(b'data:{"a": 1}\n\n') == {"a": 1}


def test_extract_data_json_ignores_comment_event_and_id_fields():
    from gateway.sse_parser import extract_data_json
    event = (b": keepalive\n"
             b"event: delta\n"
             b"id: 7\n"
             b"retry: 3000\n"
             b'data: {"v": {"conversation_id": "cid-9"}}\n\n')
    assert extract_data_json(event) == {"v": {"conversation_id": "cid-9"}}


def test_extract_data_json_comment_containing_data_is_not_a_field():
    """A comment whose text happens to start with `data:` must be ignored."""
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b':data: {"type": "message"}\n\n') is None


def test_extract_data_json_crlf_and_cr_line_endings():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b'event: delta\r\ndata: {"a": 1}\r\n\r\n') == {"a": 1}
    assert extract_data_json(b'event: delta\rdata: {"a": 1}\r\r') == {"a": 1}


def test_extract_data_json_non_object_json_returns_none():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b'data: [1, 2, 3]\n\n') is None
    assert extract_data_json(b'data: "just a string"\n\n') is None


def test_extract_data_json_no_data_field_returns_none():
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(b"event: ping\nid: 1\n\n") is None


def test_extract_data_json_utf8_payload_roundtrips():
    from gateway.sse_parser import extract_data_json
    event = 'data: {"title": "你好世界"}\n\n'.encode("utf-8")
    assert extract_data_json(event) == {"title": "你好世界"}


# ---------------------------------------------------------------------------
# Regression: real UTF-8 byte splits across transport chunks
# ---------------------------------------------------------------------------

def test_utf8_split_inside_multibyte_char_reassembles_exactly():
    """Split mid-character: one event comes out and it decodes cleanly."""
    raw = 'data: {"title": "你好"}\n\n'.encode("utf-8")
    # Byte 17 lands inside the 3-byte encoding of 你.
    result = _events([raw[:17], raw[17:]])
    assert result == [raw]
    from gateway.sse_parser import extract_data_json
    assert extract_data_json(result[0]) == {"title": "你好"}


def test_utf8_split_at_every_byte_offset_yields_same_stream():
    """Exhaustive: any single split point reproduces the same event list."""
    raw = 'data: {"t": "你好世界"}\n\ndata: {"t": "café"}\n\n'.encode("utf-8")
    expected = _events([raw])
    assert len(expected) == 2
    for i in range(1, len(raw)):
        assert _events([raw[:i], raw[i:]]) == expected, f"split at byte {i}"


def test_async_utf8_split_inside_multibyte_char():
    raw = 'data: {"title": "世界"}\n\n'.encode("utf-8")
    assert asyncio.run(_async_events([raw[:18], raw[18:]])) == [raw]


# ---------------------------------------------------------------------------
# Regression: content_generator tracks history from split / coalesced events
# ---------------------------------------------------------------------------

class _FakeUpstream:
    """Minimal stand-in for the curl_cffi streaming response."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


def _run_content_generator(chunks, token="seed-token"):
    """Drive content_generator with a fake upstream; return (yielded, saved)."""
    from gateway import reverseProxy

    saved = []

    async def _drain():
        out = []
        async for item in reverseProxy.content_generator(_FakeUpstream(chunks), token, True):
            out.append(item)
        return out

    original = reverseProxy.save_conversation
    reverseProxy.save_conversation = lambda tok, cid, title=None: saved.append((tok, cid, title))
    # save_conversation is stubbed, so conversation_map stays empty; the real
    # code reads back the stored title from it, which yields None here.
    try:
        yielded = asyncio.run(_drain())
    finally:
        reverseProxy.save_conversation = original
    return yielded, saved


def test_content_generator_yields_events_unchanged():
    chunks = [b"data: {\"conversation_id\": \"cid-1\"}\n\n", b": ping\n\n", b"data: [DONE]\n\n"]
    yielded, _ = _run_content_generator(chunks)
    assert b"".join(yielded) == b"".join(chunks)


def test_content_generator_tracks_conversation_id_split_across_chunks():
    """The id-bearing event arrives in three pieces — it must still be saved."""
    raw = b'data: {"conversation_id": "cid-split", "title": null}\n\n'
    yielded, saved = _run_content_generator([raw[:10], raw[10:30], raw[30:]])
    assert b"".join(yielded) == raw
    assert saved == [("seed-token", "cid-split", None)]


def test_content_generator_tracks_from_coalesced_events():
    """Several events in one chunk: id from the first, title from a later one."""
    chunk = (b'data: {"conversation_id": "cid-c"}\n\n'
             b'data: {"type": "message"}\n\n'
             b'data: {"title": "Coalesced"}\n\n')
    yielded, saved = _run_content_generator([chunk])
    assert b"".join(yielded) == chunk
    assert saved == [("seed-token", "cid-c", None), ("seed-token", "cid-c", "Coalesced")]


def test_content_generator_tracks_conversation_id_from_v_delta():
    """`event: delta` payloads carry the id inside the `v` dict."""
    chunk = b'event: delta\ndata: {"v": {"conversation_id": "cid-v"}}\n\n'
    yielded, saved = _run_content_generator([chunk])
    assert yielded == [chunk]
    assert saved == [("seed-token", "cid-v", None)]


def test_content_generator_skips_title_without_conversation_id():
    """A title-only stream must not write a record with no conversation id."""
    chunk = b'data: {"title": "Orphan"}\n\n'
    yielded, saved = _run_content_generator([chunk])
    assert yielded == [chunk]
    assert saved == []


def test_content_generator_ignores_comments_and_done():
    chunks = [b": keepalive\n\n", b"data: [DONE]\n\n", b"data: not json\n\n"]
    yielded, saved = _run_content_generator(chunks)
    assert b"".join(yielded) == b"".join(chunks)
    assert saved == []


def test_content_generator_preserves_incomplete_event_at_eof():
    chunks = [b'data: {"conversation_id": "cid-eof"}\n\n', b'data: {"partial"']
    yielded, saved = _run_content_generator(chunks)
    assert b"".join(yielded) == b"".join(chunks)
    assert yielded[-1] == b'data: {"partial"'
    assert saved == [("seed-token", "cid-eof", None)]


def test_content_generator_does_not_track_for_api_style_token():
    """45-char tokens are direct-API clients — no history bookkeeping."""
    chunk = b'data: {"conversation_id": "cid-x"}\n\n'
    yielded, saved = _run_content_generator([chunk], token="a" * 45)
    assert yielded == [chunk]
    assert saved == []


# ---------------------------------------------------------------------------
# Regression: generic two-line-terminator event boundary
#
# SSE recognises CRLF, CR and LF as line terminators, and an event ends at a
# blank line -- i.e. ANY two consecutive terminators, not only the three
# homogeneous pairs.  b"\n\r" is a LF-terminated data line followed by a
# CR-terminated blank line and therefore ends an event.
# ---------------------------------------------------------------------------

def test_lf_then_cr_is_an_event_boundary():
    assert _events([b"data: a\n\rdata: b\n\n"]) == [b"data: a\n\r", b"data: b\n\n"]


def test_lf_then_crlf_is_a_single_boundary():
    # b"\n\r\n" is LF + CRLF = exactly two terminators; the CRLF must not be
    # split into a bare CR boundary plus a stray leading LF on the next event.
    assert _events([b"data: a\n\r\ndata: b\n\n"]) == [b"data: a\n\r\n", b"data: b\n\n"]


def test_cr_then_lf_alone_is_not_a_boundary():
    # b"\r\n" is ONE terminator (CRLF), so this is a single unterminated event.
    assert _events([b"data: a\r\ndata: b\r\n"]) == [b"data: a\r\ndata: b\r\n"]


def test_trailing_cr_at_chunk_edge_is_not_split_prematurely():
    # The chunk boundary falls inside a CRLF: the parser must wait for the LF
    # instead of treating the dangling CR as a CR-only blank line.
    assert _events([b"data: a\n\r", b"\ndata: b\n\n"]) == [b"data: a\n\r\n", b"data: b\n\n"]


def test_lf_cr_boundary_at_every_split_offset():
    raw = b"data: a\n\rdata: b\n\n"
    expected = [b"data: a\n\r", b"data: b\n\n"]
    for i in range(len(raw) + 1):
        assert _events([raw[:i], raw[i:]]) == expected, f"split at {i}"


def test_async_lf_cr_boundary():
    assert asyncio.run(_async_events([b"data: a\n\rdata: b\n\n"])) == [b"data: a\n\r", b"data: b\n\n"]


def test_async_trailing_cr_at_chunk_edge():
    assert asyncio.run(_async_events([b"data: a\n\r", b"\ndata: b\n\n"])) == [b"data: a\n\r\n", b"data: b\n\n"]


# ---------------------------------------------------------------------------
# Regression: leading whitespace in the data value is valid JSON
#
# SSE strips exactly ONE space after "data:".  Upstream sending "data:  {...}"
# (two spaces) leaves a leading space in the buffer, which is insignificant
# JSON whitespace -- dropping the event loses a real native event.
# ---------------------------------------------------------------------------

def test_extract_data_json_leading_whitespace_after_single_space_strip():
    event = b'data:  {"type":"resume_conversation_token"}\n\n'
    assert extract_data_json(event) == {"type": "resume_conversation_token"}


def test_extract_data_json_leading_tab_is_parsed():
    assert extract_data_json(b'data: \t{"a": 1}\n\n') == {"a": 1}


def test_extract_data_json_trailing_whitespace_is_parsed():
    assert extract_data_json(b'data: {"a": 1}   \n\n') == {"a": 1}


def test_extract_data_json_whitespace_only_data_returns_none():
    assert extract_data_json(b"data:   \n\n") is None


def test_extract_data_json_done_with_extra_space_returns_none():
    assert extract_data_json(b"data:  [DONE]\n\n") is None


# ---------------------------------------------------------------------------
# Regression: f_conversation must propagate the upstream failure status
#
# StreamingResponse defaults to 200.  An upstream 403/429 that still carries a
# text/event-stream content-type would therefore reach the browser as a
# successful empty stream, which the UI renders as a silently-truncated reply
# instead of an error.
# ---------------------------------------------------------------------------

def test_f_conversation_streaming_response_carries_upstream_status():
    import inspect

    from gateway import f_conversation_gateway

    source = inspect.getsource(f_conversation_gateway.f_conversation)
    streaming_call = source.split("StreamingResponse(", 1)[1].split("return response", 1)[0]
    assert "status_code=r.status_code" in streaming_call, (
        "StreamingResponse must pass status_code=r.status_code; it defaults to 200"
    )
