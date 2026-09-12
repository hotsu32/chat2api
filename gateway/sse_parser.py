"""SSE event-boundary parser for the f/conversation stream.

SSE events are separated by a blank line (``\\n\\n``, ``\\r\\n\\r\\n``, or
``\\r\\r``).  TCP is a byte stream: a single delivery can contain zero, one, or
many complete events, and an event can be split across an arbitrary number of
deliveries.  ``iter_sse_events`` and ``iter_sse_events_async`` reassemble
transport chunks into whole events so callers never see a partial event.

Design constraints (from the project plan):
- No polling; no buffering until EOF.
- Must handle arbitrary chunk splits including inside a UTF-8 multibyte char.
- Must pass through comments (``:``) unchanged.
- Incomplete event at EOF is yielded as-is (not silently dropped).
"""
import json
import re
from typing import AsyncIterator, Iterable, Iterator, Optional

# SSE recognises exactly three line terminators: CRLF, CR, LF.  str.splitlines()
# would also break on \v, \f, \x1c and friends, which are legal JSON payload
# bytes — splitting on them would corrupt the data buffer.
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

# One line terminator.  A bare CR only counts when it is *not* followed by LF,
# otherwise the regex engine could backtrack and split a single CRLF into
# CR + LF — which would turn the ordinary "data: a\r\ndata: b" into a bogus
# event boundary.
_TERMINATOR = rb"(?:\r\n|\n|\r(?!\n))"
# An event ends at a blank line, i.e. *any* two consecutive terminators —
# not only the homogeneous \n\n / \r\n\r\n / \r\r pairs.  Mixed pairs such as
# "\n\r" (LF-terminated data line + CR-terminated blank line) are equally valid
# and appear when an upstream or proxy rewrites line endings mid-stream.
_EVENT_END_RE = re.compile(_TERMINATOR + _TERMINATOR)


def iter_sse_events(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Yield whole SSE events from an iterable of raw transport chunks.

    Each yielded ``bytes`` value is one complete SSE event including its
    terminating blank line.  An incomplete event at EOF (stream truncated
    without a final blank line) is yielded as a single item.
    """
    buf = b""
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        buf = yield from _drain_events(buf)
    # Flush any partial event at EOF.
    if buf:
        yield buf


async def iter_sse_events_async(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Async variant of iter_sse_events."""
    buf = b""
    async for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while True:
            end = _find_event_end(buf)
            if end == -1:
                break
            yield buf[:end]
            buf = buf[end:]
    if buf:
        yield buf


def _drain_events(buf: bytes):
    """Pop complete events from *buf*, yield each, return the leftover."""
    while True:
        end = _find_event_end(buf)
        if end == -1:
            break
        yield buf[:end]
        buf = buf[end:]
    return buf


def _find_event_end(buf: bytes) -> int:
    """Return the index *after* the earliest event-terminating blank line, or -1.

    An event ends at a blank line: any two consecutive line terminators, where
    each terminator is CRLF, LF or a bare CR.  Matching generically (rather
    than testing three fixed byte pairs by priority) is what makes mixed
    endings work — a stream that switches from ``\\n`` to ``\\r\\n`` mid-flight
    produces the pair ``\\n\\r\\n``, and ``\\n\\r`` is just as much a blank line
    as ``\\n\\n``.

    Returns -1 when the buffer ends on a CR that may still turn out to be the
    first half of a CRLF.  Emitting there would cut the event one byte early
    and re-emit the stray LF as the next event's first byte.
    """
    match = _EVENT_END_RE.search(buf)
    if match is None:
        return -1
    end = match.end()
    if end == len(buf) and buf.endswith(b"\r"):
        # Ambiguous tail: need the next byte to know if this is CR or CRLF.
        return -1
    return end


def is_complete_event(event: bytes) -> bool:
    """Distinguish a terminated event from the partial EOF item we preserve."""
    match = _EVENT_END_RE.search(event)
    return match is not None and match.end() == len(event)


def extract_data(event: bytes) -> Optional[str]:
    """Parse the SSE data buffer, preserving multi-line field semantics.

    Follows the SSE field-parsing rules: every ``data`` field value in the
    event is collected and the values are joined with a single LF; exactly one
    optional space after the field's colon is stripped; comment lines (``:``)
    and ``event:``/``id:``/``retry:`` fields are ignored.  The joined buffer is
    parsed **once**.

    Returns None for events with no data field; includes non-JSON terminals.
    """
    try:
        text = event.decode("utf-8", errors="replace")
    except Exception:
        return None
    values = []
    for line in _LINE_SPLIT_RE.split(text):
        if not line or line.startswith(":"):
            # Blank separator line or comment — neither carries a field.
            continue
        field, sep, value = line.partition(":")
        if not sep:
            # A line with no colon is a field name with an empty value.
            field, value = line, ""
        elif value.startswith(" "):
            # Remove exactly one leading space, not all of them.
            value = value[1:]
        if field == "data":
            values.append(value)
    if not values:
        return None
    # SSE strips exactly one space after the colon, so an upstream writing
    # "data:  {...}" leaves a leading space here.  That is insignificant JSON
    # whitespace — treating it as non-JSON would silently drop a real native
    # event (this is what hid resume_conversation_token events).
    return "\n".join(values)


def extract_data_json(event: bytes) -> Optional[dict]:
    """Decode the joined data buffer once; non-object data returns None."""
    data = extract_data(event)
    if data is None:
        return None
    payload = data.strip()
    if not payload or payload == "[DONE]" or not payload.startswith("{"):
        return None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
