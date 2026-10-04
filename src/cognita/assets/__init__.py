"""First-class static PNG asset support for Cognita 7.1.

The isolated OCR interpreter imports :mod:`cognita.assets.ocr_worker` as a
module.  Keep package initialization dependency-light so that this path does
not require Cognita's service-only packages (notably pydantic) in the
dedicated EasyOCR environment.  Host-side services retain the same public
attributes through the lazy exports below.
"""

from __future__ import annotations

import importlib

from .metadata import canonical_metadata, search_projection
from .models import AssetError, AssetRecord, PngFacts
from .ocr_models import OCRRegion, OCRWorkerPayload
from .ocr_worker import OCRWorkerRunner
from .png import embed_metadata, scan_png

_LAZY_EXPORTS = {
    "AssetService": (".service", "AssetService"),
    "OCRService": (".ocr_service", "OCRService"),
    "OcrResultFacts": (".ocr_store", "OcrResultFacts"),
    "OcrSearchChunk": (".ocr_store", "OcrSearchChunk"),
    "OcrSourceSnapshot": (".ocr_store", "OcrSourceSnapshot"),
    "OcrStore": (".ocr_store", "OcrStore"),
    "result_facts_from_result": (".ocr_service", "result_facts_from_result"),
    "source_snapshot_from_result": (".ocr_service", "source_snapshot_from_result"),
}


def __getattr__(name: str):
    """Load host-only asset orchestration types on first access."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(importlib.import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "AssetError",
    "AssetRecord",
    "AssetService",
    "OCRRegion",
    "OCRService",
    "OCRWorkerPayload",
    "OCRWorkerRunner",
    "OcrResultFacts",
    "OcrSearchChunk",
    "OcrSourceSnapshot",
    "OcrStore",
    "PngFacts",
    "canonical_metadata",
    "embed_metadata",
    "result_facts_from_result",
    "scan_png",
    "search_projection",
    "source_snapshot_from_result",
]
