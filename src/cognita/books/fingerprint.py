"""RFC 8785 request fingerprints and Unicode grapheme boundary helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import regex
import rfc8785

from .models import RequestSpec

FINGERPRINT_VERSION = "jcs-v1"
_GRAPHEME = regex.compile(r"\X", regex.VERSION1)


def _utf8(text: str) -> bytes:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    # RFC 8785 and the book text contract reject lone surrogate code points.
    return text.encode("utf-8", errors="strict")


def sha256_text(text: str) -> str:
    """Hash the exact UTF-8 encoding of text without normalization or trimming."""
    return hashlib.sha256(_utf8(text)).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash canonical RFC 8785 bytes, rejecting non-JSON and invalid Unicode."""
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def request_fingerprint(prompt: str, request_spec: RequestSpec | Mapping[str, Any]) -> str:
    """Return the normative request hash over exact prompt hash and actual fields."""
    prompt_sha256 = sha256_text(prompt)
    if isinstance(request_spec, RequestSpec):
        spec: Mapping[str, Any] = request_spec.model_dump(mode="json", exclude_unset=True)
    elif isinstance(request_spec, Mapping):
        spec = request_spec
    else:
        raise TypeError("request_spec must be RequestSpec or a JSON object mapping")
    # Validate mappings through the same strict DTO so caller omission and
    # explicit values are preserved, while unknown keys and non-JSON structures
    # cannot enter the fingerprint.
    normalized = RequestSpec.model_validate(dict(spec), strict=True).model_dump(
        mode="json", exclude_unset=True
    )
    return canonical_json_sha256({
        "prompt_sha256": prompt_sha256,
        "request_spec": normalized,
    })


def grapheme_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return extended grapheme spans in Python code-point coordinates."""
    _utf8(text)
    return tuple(match.span() for match in _GRAPHEME.finditer(text))


def grapheme_boundaries(text: str) -> frozenset[int]:
    """Return every supported cut position, including both text endpoints."""
    spans = grapheme_spans(text)
    return frozenset((0, *(end for _, end in spans)))


def is_grapheme_boundary(text: str, offset: int) -> bool:
    """Whether offset is a valid code-point boundary between grapheme clusters."""
    return offset in grapheme_boundaries(text)


def validate_grapheme_boundary(text: str, offset: int) -> None:
    """Raise ValueError when offset splits a Unicode grapheme cluster."""
    if not is_grapheme_boundary(text, offset):
        raise ValueError("offset splits a Unicode grapheme cluster")


__all__ = [
    "FINGERPRINT_VERSION", "canonical_json_sha256", "request_fingerprint", "sha256_text",
    "grapheme_spans", "grapheme_boundaries", "is_grapheme_boundary",
    "validate_grapheme_boundary",
]
