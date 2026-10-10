"""MCP / JSON-RPC protocol rules shared by every entry point (16.1.3).

A leaf module: it imports nothing from Cognita, so the connector route, the
Workspace-only route, the proxy's batch handler and the local engine all call
the same code instead of carrying their own copies of the same rule. Before
16.1.3 each entry point kept its own copy of the message rules (on 16.1.2
both gateway routes answered a single request without an `id` with
`"id": null`, and both dropped the replies to id-less batch members; that
behavior is kept on purpose, see classify_message), the engine echoed any
protocol version a client asked for, and a malformed body could escape as
HTTP 500.

Cognita is a legacy (initialize-handshake) server for the revisions below. The
`MCP-Protocol-Version` request HEADER is deliberately NOT part of this module
and is never validated or rejected anywhere: clients that first send
`server/discover` with `MCP-Protocol-Version: 2026-07-28`, get -32601 with their
own id, and then send `initialize` work today and must keep working.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

# Newest first. The one authority for the revisions this server speaks.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
# 16.1.3: the two oldest revisions are still answered as requested although
# they are NOT in SUPPORTED_PROTOCOL_VERSIONS. Cognita does not implement the
# HTTP+SSE transport those revisions defined, but a client that asks for one
# of them over Streamable HTTP worked on 16.1.2 (initialize echoed any version)
# and must keep working: a client that checks the answer against what it asked
# for would otherwise disconnect. Deliberately separate so the supported list
# above stays an honest statement of what the server speaks.
LEGACY_ECHOED_PROTOCOL_VERSIONS = ("2024-11-05", "2024-10-07")
# What an `initialize` that names no protocolVersion has always been answered
# with; kept so such a client sees exactly what it saw before 16.1.3.
ABSENT_PROTOCOL_VERSION = "2025-03-26"


def negotiate_protocol_version(params: Any) -> str:
    """The version an `initialize` is answered with (Lifecycle, Version Negotiation).

    The spec: answer with the requested version if the server supports it,
    otherwise with one it does support. Before 16.1.3 any string was echoed
    back, including a revision this server does not implement. A request that
    names no version keeps 2025-03-26. The three supported revisions and the
    two legacy ones (LEGACY_ECHOED_PROTOCOL_VERSIONS) are answered as
    requested. Any other present value (a future revision such as
    2026-07-28, a typo, a non-string) is answered with the newest supported
    revision, which the client then accepts or disconnects from, as the spec
    intends.
    """
    if not isinstance(params, dict) or "protocolVersion" not in params:
        return ABSENT_PROTOCOL_VERSION
    requested = params["protocolVersion"]
    if isinstance(requested, str) and (
        requested in SUPPORTED_PROTOCOL_VERSIONS
        or requested in LEGACY_ECHOED_PROTOCOL_VERSIONS
    ):
        return requested
    return LATEST_PROTOCOL_VERSION


# ---------------------------------------------------------------- message kinds

REQUEST = "request"
NOTIFICATION = "notification"
RESPONSE = "response"
INVALID = "invalid"


class _NoId:
    """Marks a reply whose id cannot be known: the `id` member is omitted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_ID"


# Not None: for a request that omitted its id (or sent null) the reply has
# always echoed `"id": null`, and that stays byte-for-byte what it was.
NO_ID = _NoId()


@dataclass(frozen=True, slots=True)
class MessageKind:
    """How one JSON-RPC message is treated. `reply_id` is the id an INVALID
    message's error reply may echo: a valid id from the message, else NO_ID,
    which means the reply carries no `id` member at all (the MCP schema never
    allows null)."""

    kind: str
    method: str | None = None
    reply_id: Any = NO_ID
    reason: str = ""


def is_valid_request_id(value: Any) -> bool:
    """An id worth echoing in an error reply to a malformed message: a string
    or a finite number. Not bool (a Python bool is an int), not null, not
    NaN/Infinity (Python's JSON parser accepts those literals), not an object
    or array. This is NOT the test for whether a message is executed; see
    classify_message, which accepts every id except a non-finite float."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str) or isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def id_encodes_as_json(value: Any) -> bool:
    """True when an id can be written back as strict JSON in UTF-8.

    Python's JSON parser accepts NaN / Infinity (also nested in an array or
    object id) and a JSON-escaped lone surrogate ("\\ud800"); the reply
    encoder then raises (ValueError for the first, UnicodeEncodeError for the
    second) and the exchange became HTTP 500. Every id that encodes is still
    accepted and echoed exactly as before; one that does not is refused as an
    invalid request (see classify_message).
    """
    if value is None or isinstance(value, (bool, int)):
        return True  # the common ids; no need to encode them to know
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except ValueError:  # NaN / Infinity (ValueError) and a surrogate (UnicodeEncodeError)
        return False
    return True


def classify_message(message: Any) -> MessageKind:
    """Decide what a message IS (Base Protocol; Transports, Sending Messages).
    Single messages and batch members alike, on every route.

    - a string `method` starting with `notifications/`: a notification (202,
      no body), whatever its `id` is;
    - any other string `method`: a REQUEST, executed and answered exactly as
      before 16.1.3, whatever its `id` is (missing, null, string, number, bool,
      object, array); the reply echoes the id the way 16.1.2 did, so a missing
      id comes back as `"id": null`. The ONLY refused ids are those that cannot
      be written back as strict JSON in UTF-8 (id_encodes_as_json): a
      non-finite float (NaN, Infinity, -Infinity) or one nested in an array or
      object id, and a string holding a lone surrogate. They crashed response
      encoding with HTTP 500: that is an invalid request (-32600) with no `id`
      member;
    - no `method`, but `result` or `error`: a response from the client,
      accepted (202, no body); an object with both is classified by `method`;
    - anything else: an invalid request (-32600).

    16.1.3, deliberate leniency: JSON-RPC says a message without an id is a
    notification and must not be answered. Cognita keeps answering such
    requests (and requests with a null or odd id) because clients that omit
    the id worked before and must keep working. Do not "fix" this.
    """
    if not isinstance(message, dict):
        return MessageKind(INVALID, reason="not_an_object")
    raw_id = message.get("id")
    non_finite = isinstance(raw_id, float) and not math.isfinite(raw_id)
    encodes = id_encodes_as_json(raw_id)
    usable_id = raw_id if encodes and is_valid_request_id(raw_id) else NO_ID
    if "method" in message:
        method = message["method"]
        if not isinstance(method, str):
            return MessageKind(INVALID, reply_id=usable_id, reason="method_not_a_string")
        if method.startswith("notifications/"):
            return MessageKind(NOTIFICATION, method=method)
        if non_finite:
            return MessageKind(INVALID, method=method, reason="non_finite_id")
        if not encodes:
            return MessageKind(INVALID, method=method, reason="id_not_encodable")
        return MessageKind(REQUEST, method=method, reply_id=raw_id)
    if "result" in message or "error" in message:
        return MessageKind(RESPONSE)
    return MessageKind(INVALID, reply_id=usable_id, reason="no_method")


def error_body(msg_id: Any, code: int, message: str) -> dict:
    """A JSON-RPC error object. NO_ID omits the `id` member: the MCP schema
    never allows `"id": null`, and 2025-11-25 makes the member optional. Any
    other id, None included, is echoed (a request that sent no id has always
    been answered `"id": null`). The key order (jsonrpc, id, error) is what the
    server has always sent."""
    body: dict[str, Any] = {"jsonrpc": "2.0"}
    if msg_id is not NO_ID:
        body["id"] = msg_id
    body["error"] = {"code": code, "message": message}
    return body


# 16.1.3: what a request is answered with when a reply cannot be encoded as
# UTF-8 because the request carried text that is not valid Unicode (a JSON
# "\ud800" escape yields a lone surrogate, which json.loads accepts and the
# response encoder refuses). It used to be an HTTP 500. No `id` member: the id
# may be the very text that cannot be written back.
UNENCODABLE_TEXT_MESSAGE = "Invalid request: the request contains text that is not valid Unicode"


def unencodable_text_reply() -> dict:
    """The JSON-RPC error a request is answered with when its reply cannot be
    encoded (see UNENCODABLE_TEXT_MESSAGE): -32600, no `id` member."""
    return error_body(NO_ID, -32600, UNENCODABLE_TEXT_MESSAGE)


def log_safe(value: Any, limit: int = 60) -> str:
    """A client-controlled value as it may appear in a log line (16.1.3).

    A client chooses these values (method names, client name and version, the
    requested protocol version, ids, tool names, even the URL path), and a
    percent-decoded `%0A` or a JSON `\\n` would otherwise write a second,
    forged log line. Whitespace of every kind is collapsed to single spaces,
    every other non-printable character (control characters, lone surrogates,
    format characters) becomes `?`, and the result is cut to `limit`
    characters. Only a string, a number, a bool or null is rendered by value;
    a list or object shows its type (`<dict>`), never its content, because
    str() of it would print request content into the log.
    """
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float)):
        text = str(value)
    else:
        text = f"<{type(value).__name__}>"
    # Bound first so a megabyte-long value costs no more than a short one.
    text = " ".join(text[: limit * 4].split())
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:limit]


# ----------------------------------------------------------------- body parsing

class BodyParseError(ValueError):
    """The request body is not parseable JSON. `cause` names why, for the log."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


# No real MCP message nests anywhere near this (a batch of calls with edit lists
# is under ten levels). It is far below what exhausts the interpreter, so a body
# past it is refused as a parse error BEFORE the recursive code that handles
# parsed messages (project-routing checks, JSON re-encoding, log rendering)
# can raise RecursionError on it. Measured 2026-10-10: a body that parsed fine
# at 600 levels of nested `arguments` raised RecursionError in the gateway's
# recursive project check, an HTTP 500, long after json.loads had succeeded.
MAX_NESTING_DEPTH = 128


def _nested_deeper_than(value: Any, limit: int) -> bool:
    """Iterative (a recursive walk would hit the very limit it guards)."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children
                     if isinstance(child, (dict, list)))
    return False


def parse_body(raw: bytes) -> Any:
    """json.loads for a request body, raising only BodyParseError.

    Before 16.1.3 only JSONDecodeError was caught, so a body that is not valid
    UTF-8 (UnicodeDecodeError) or is nested deeply enough to exhaust the
    parser's recursion limit (RecursionError) escaped as HTTP 500. Both are
    ordinary parse errors (-32700), and so is nesting past MAX_NESTING_DEPTH.
    """
    try:
        value = json.loads(raw)
    except RecursionError as exc:
        raise BodyParseError("nested_too_deeply") from exc
    except UnicodeError as exc:
        raise BodyParseError("invalid_utf8") from exc
    except ValueError as exc:
        raise BodyParseError("invalid_json") from exc
    # The cheap byte count means ordinary bodies never pay for the walk.
    if (raw.count(b"[") + raw.count(b"{") > MAX_NESTING_DEPTH
            and _nested_deeper_than(value, MAX_NESTING_DEPTH)):
        raise BodyParseError("nested_too_deeply")
    return value
