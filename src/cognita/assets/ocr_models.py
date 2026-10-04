"""Small, transport-independent OCR value types.

These types describe the public OCR contract and the subprocess result.  The
persistence layer owns its own ``OcrResultFacts`` DTO; the service converts the
mapping returned here to that DTO at its publication boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OCRRegion:
    """One detected region in original-image pixel coordinates."""

    text: str
    bbox: tuple[int, int, int, int]
    polygon: tuple[tuple[int, int], ...]
    confidence: float | None
    paragraph: int
    line: int
    order: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": list(self.bbox),
            "polygon": [list(point) for point in self.polygon],
            "confidence": self.confidence,
            "paragraph": self.paragraph,
            "line": self.line,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class OCRWorkerPayload:
    """Validated result returned by one isolated OCR worker invocation."""

    width: int
    height: int
    regions: tuple[OCRRegion, ...]
    device: str
    backend: str
    engine_name: str
    engine_version: str
    model_fingerprint: str
    device_binding: str = ""
    pipeline_version: int = 1
