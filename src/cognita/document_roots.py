"""The documents roots the installer bound into the container, and how to show them to a person.

DESIGN-LINUX-INSTALLER.md 7.2 (roots) and 19.2 (displays).  Two environment variables, both written
by release.py's folders fragment (``compose.folders.yaml``) for the ``local`` target only:

``COGNITA_DOCUMENT_ROOTS``
    A JSON list of absolute container paths, one per documents root.  Unchanged since 14.1.0's first
    draft, so an older image after a rollback still reads it.
``COGNITA_DOCUMENT_ROOT_DISPLAYS``
    A JSON object mapping a root's container path to the text a person knows it by (for example a
    Windows path such as ``D:\\Documents``).  Written only when at least one root has a display; an image
    that does not know it ignores it, and with it absent the display of every path is the path itself.

The display is for PEOPLE.  Identifying a root (``path-info`` ``root``, the New Project request, the index)
always uses the container path.  Everything here is read on every call so a test can set the variables;
a malformed value is logged and treated as absent, never an exception.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

ROOTS_VARIABLE = "COGNITA_DOCUMENT_ROOTS"
DISPLAYS_VARIABLE = "COGNITA_DOCUMENT_ROOT_DISPLAYS"


def _load_json(name: str) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        log.warning("%s is not valid JSON (%s); ignoring it", name, exc)
        return None


def configured_document_roots() -> list[str]:
    """The documents roots the installer bound into the container (installer design 7.2).

    ``COGNITA_DOCUMENT_ROOTS`` is a JSON list of absolute paths written by the Linux installer's Compose
    fragment. Unset (kei, developer runs, the old installs) means no roots, and the New Project form keeps
    its absolute-path field. A malformed value is logged and treated as no roots.
    """
    loaded = _load_json(ROOTS_VARIABLE)
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        log.warning("%s is not a JSON list (%s); treating it as no roots", ROOTS_VARIABLE, type(loaded).__name__)
        return []
    return [item for item in loaded if isinstance(item, str) and item]


def configured_document_displays() -> dict[str, str]:
    """Root path -> display text.  Empty when the variable is unset or malformed."""
    loaded = _load_json(DISPLAYS_VARIABLE)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        log.warning("%s is not a JSON object (%s); ignoring it", DISPLAYS_VARIABLE, type(loaded).__name__)
        return {}
    return {key: value for key, value in loaded.items()
            if isinstance(key, str) and key and isinstance(value, str) and value}


def roots_with_displays() -> list[dict[str, str]]:
    """``[{"path", "display"}]`` for every configured root; the display is the path when none was given."""
    displays = configured_document_displays()
    return [{"path": root, "display": displays.get(root, root)} for root in configured_document_roots()]


def _forward(path: str) -> str:
    """Compare on forward slashes so a Windows-spelled path in a test matches a POSIX-spelled root.
    Container paths never contain a backslash in practice."""
    return path.replace("\\", "/")


def _separator(display: str) -> str:
    """The separator a display uses: a backslash if it holds one (a Windows path), else a slash."""
    return "\\" if "\\" in display else "/"


def display_for(path: Any) -> str:
    """How a person knows ``path``: the display of the root it lies under, plus the part below it.

    The remainder is joined with the display's own separator (``D:\\Documents`` + ``Manuals/Air`` ->
    ``D:\\Documents\\Manuals\\Air``). A path under no root, or under a root with no display, comes back
    as it was given. The longest matching root wins, though roots never nest.
    """
    text = os.fspath(path)
    displays = configured_document_displays()
    if not displays:
        return text
    probe = _forward(text).rstrip("/") or "/"
    best: tuple[int, str, str] | None = None
    for root, display in displays.items():
        root_norm = _forward(root).rstrip("/") or "/"
        if probe == root_norm:
            rest = ""
        elif probe.startswith(root_norm.rstrip("/") + "/"):
            rest = probe[len(root_norm.rstrip("/")) + 1:]
        else:
            continue
        if best is None or len(root_norm) > best[0]:
            best = (len(root_norm), display, rest)
    if best is None:
        return text
    _length, display, rest = best
    if not rest:
        return display
    separator = _separator(display)
    return display.rstrip("\\/") + separator + rest.replace("/", separator)
