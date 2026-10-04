"""Small standard-library loader for Cognita's packaged message catalogs."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Mapping

SUPPORTED_LOCALES = ("en-US", "es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR")
_REGIONLESS = {"es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT"}
_PLACEHOLDER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def resolve_locale(value: str | None) -> str:
    """Return a supported locale tag, falling back to U.S. English."""
    candidate = (value or "").strip().replace("_", "-")
    if candidate in SUPPORTED_LOCALES:
        return candidate
    return _REGIONLESS.get(candidate.lower(), "en-US")


def _catalog_path(locale: str, catalog_dir: str | Path | None) -> Path:
    root = Path(catalog_dir) if catalog_dir is not None else Path(__file__).parent / "web" / "locales"
    return root / f"{resolve_locale(locale)}.json"


@lru_cache(maxsize=6)
def _read_catalog(path: str) -> Mapping[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in data.items()
    ):
        raise ValueError(f"catalog must be a flat string map: {path}")
    return MappingProxyType(data)


def load_catalog(
    locale: str = "en-US", catalog_dir: str | Path | None = None
) -> Mapping[str, str]:
    """Load and cache one catalog from the package or an explicit catalog directory."""
    path = _catalog_path(locale, catalog_dir).resolve()
    try:
        return _read_catalog(str(path))
    except FileNotFoundError:
        if resolve_locale(locale) == "en-US":
            raise
        return _read_catalog(str(_catalog_path("en-US", catalog_dir).resolve()))


def _placeholders(message: str) -> set[str]:
    if _HTML_TAG.search(message):
        raise ValueError("catalog messages must not contain HTML markup")
    names = set()
    for _, field, format_spec, conversion in Formatter().parse(message):
        if field is None:
            continue
        if not _PLACEHOLDER.fullmatch(field) or format_spec or conversion:
            raise ValueError(f"unsupported catalog placeholder: {{{field}}}")
        names.add(field)
    return names


def translate(
    locale: str,
    message_id: str,
    values: Mapping[str, object] | None = None,
    catalog_dir: str | Path | None = None,
) -> str:
    """Format a message by stable ID, using English for an absent ID."""
    selected = load_catalog(locale, catalog_dir)
    english = selected if resolve_locale(locale) == "en-US" else load_catalog("en-US", catalog_dir)
    message = selected.get(message_id, english.get(message_id))
    if message is None:
        raise KeyError(f"unknown message ID: {message_id}")
    supplied = dict(values or {})
    expected = _placeholders(message)
    if set(supplied) != expected:
        missing, extra = sorted(expected - supplied.keys()), sorted(supplied.keys() - expected)
        raise ValueError(f"placeholder mismatch for {message_id}: missing={missing}, extra={extra}")
    return message.format_map(supplied)


def validate_catalogs(catalog_dir: str | Path | None = None) -> None:
    """Raise when supported catalogs differ in keys or named placeholders."""
    english = _read_catalog(str(_catalog_path("en-US", catalog_dir).resolve()))
    signature = {key: _placeholders(value) for key, value in english.items()}
    for locale in SUPPORTED_LOCALES[1:]:
        catalog = _read_catalog(str(_catalog_path(locale, catalog_dir).resolve()))
        if set(catalog) != set(english):
            missing = sorted(set(english) - set(catalog))
            extra = sorted(set(catalog) - set(english))
            raise ValueError(f"{locale} catalog keys differ: missing={missing}, extra={extra}")
        for key, value in catalog.items():
            if _placeholders(value) != signature[key]:
                raise ValueError(f"{locale} placeholder set differs for {key}")
