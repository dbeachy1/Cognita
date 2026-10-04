"""Strict tool-argument validation (5.0 §2).

An argument absent from a tool's `inputSchema` used to be dropped in silence:
`list_documents(path_prefix=...)` returned all 319 documents and reported
`status: "success"`. The wasted call is not the damage — the damage is a client
that believes it filtered and did not, and has no way to find out. Every
unrecognized key is now a refusal that names the key it rejected and lists what
the tool actually accepts, so a wrong guess costs one round trip instead of a
wrong answer.

The check lives here, next to no logic of its own, because BOTH tool layers
need it and neither may import the other: engine_local.py validates the engine
tools against ENGINE_TOOL_DEFS, proxy.py validates the gateway-served tools
against their own defs (editing/reading/backups/selftest).
"""

from __future__ import annotations


def wire_error(exc: BaseException) -> str:
    """Render an exception for a CLIENT payload, without leaking host paths.

    `str(OSError)` renders as ``[Errno 2] No such file or directory:
    'C:\\\\private\\\\documents\\\\notes.md'`` — the server's directory layout
    and private file location inside a message the client
    is told to show the user verbatim. That text then lives in connector
    transcripts and shared chats. The caller needs to know WHAT went wrong, never
    WHERE on the host it went wrong: every tool payload already names the file in
    a project-relative `filepath` field.

    Same module as the argument gate for the same reason: both tool layers need
    it and neither may import the other.
    """
    if isinstance(exc, OSError) and exc.strerror:
        # An OS-RAISED OSError carries .strerror ("Permission denied") plus
        # .filename, and str() splices the filename in. .strerror alone is the
        # reason without the path. An OSError built from a plain message instead
        # has strerror None and no filename, so str() is just that message and is
        # safe — falling through to it below keeps the useful detail rather than
        # flattening every such error to its class name.
        return f"{type(exc).__name__}: {exc.strerror}"
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def accepted_arguments(tool_def: dict) -> list[str]:
    """The argument names a tool's inputSchema declares, sorted."""
    schema = tool_def.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    return sorted(properties)


def unknown_arguments(tool_def: dict, args: dict) -> list[str]:
    """Keys present in `args` that the tool's schema does not declare."""
    schema = tool_def.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):  # malformed def: nothing to check against
        return []
    return sorted(k for k in args if k not in properties)


def reject_unknown_arguments(tool_def: dict, args: dict) -> dict | None:
    """None when every argument is recognized, else the error payload to return.

    The message is addressed to the model on the other end of the connector: it
    states that NOTHING ran (so the caller does not assume a partial effect),
    names the rejected keys, and lists the accepted ones so the retry can be
    built without a second round trip to tools/list.
    """
    if not isinstance(args, dict):
        return None
    extra = unknown_arguments(tool_def, args)
    if not extra:
        return None
    name = tool_def.get("name", "?")
    accepted = accepted_arguments(tool_def)
    return {
        "status": "error",
        "reason": "unknown_argument",
        "message": (
            f"{name} does not accept {', '.join(repr(k) for k in extra)}. "
            "NOTHING was executed — the call was refused rather than run with the "
            "argument dropped, because a silently ignored filter returns a wrong "
            "answer that looks like a right one. "
            f"Accepted arguments: {', '.join(accepted) if accepted else '(none)'}."
        ),
        "tool": name,
        "rejected_arguments": extra,
        "accepted_arguments": accepted,
    }


# JSON Schema type -> the Python types a decoded JSON value can arrive as.
# `integer` accepts bool deliberately-not: True is an int in Python and a client
# sending true for max_results means something else entirely.
_JSON_TYPES: dict[str, tuple] = {
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "array": (list,),
    "object": (dict,),
}


def reject_wrong_types(tool_def: dict, args: dict) -> dict | None:
    """None when every argument matches its declared `type`, else the refusal.

    The schema declares a type for every argument and nothing enforced it. The
    handlers do `int(args.get("max_results") or 5)`, so `max_results: "abc"`
    raised ValueError out of the dispatcher and came back as
    `reason: "internal_error"` with the raw exception text — the opposite of the
    contract 5.0 §2 established for argument NAMES, which is that a refusal names
    what it rejected and why. A client sending `max_results: "10"` out of a
    JSON-stringified form deserves the same clear answer as one sending an
    unknown key, not a generic internal error.

    Deliberately narrow: it checks the declared scalar/container type and nothing
    else — no enum, range or format validation — because a stricter check here
    could refuse a call that used to work, and the wire contract is frozen.
    """
    if not isinstance(args, dict):
        return None
    schema = tool_def.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    wrong = []
    for key, value in args.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        allowed = _JSON_TYPES.get(spec.get("type"))
        if allowed is None or value is None:
            continue
        if isinstance(value, bool) and spec.get("type") in ("integer", "number"):
            wrong.append((key, spec["type"], "boolean"))
            continue
        if not isinstance(value, allowed):
            wrong.append((key, spec["type"], type(value).__name__))
    if not wrong:
        return None
    name = tool_def.get("name", "?")
    detail = "; ".join(f"{k!r} expects {want}, got {got}" for k, want, got in wrong)
    return {
        "status": "error",
        "reason": "invalid",
        "message": (
            f"{name} was called with the wrong type for {len(wrong)} argument(s): {detail}. "
            "NOTHING was executed. Send the declared JSON type — a quoted \"10\" is a "
            "string, not an integer."
        ),
        "tool": name,
        "invalid_arguments": [k for k, _w, _g in wrong],
    }
