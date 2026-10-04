#!/usr/bin/env python3
"""Generate deterministic, synthetic PNG fixtures for the 10.0 OCR gate.

The fixtures contain no user data.  They use the system's DejaVu Sans font on
KEI (Bitstream Vera derivative, permissively licensed) and record the exact
font source and SHA-256 beside the generated images. Repository fonts use a
relative path; an external font records only its filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "ocr_qualification"


def _font_path() -> Path:
    configured = os.environ.get("COGNITA_OCR_FONT")
    candidates = [
        ROOT / "tests" / "fixtures" / "ocr_qualification" / "DejaVuSans.ttf",
        Path(configured) if configured else None,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError("set COGNITA_OCR_FONT to a licensed TrueType font")


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit encoding options make output reproducible across repeated runs
    # with the same Pillow/libpng build.
    image.save(path, format="PNG", optimize=False, compress_level=9)


def _text_fixture(font_path: Path, name: str, lines: list[str], *, size: int,
                  bg: tuple[int, ...], fg: tuple[int, ...], width: int = 1600) -> tuple[Image.Image, dict]:
    font = _font(font_path, size)
    line_height = size + max(16, size // 2)
    image = Image.new("RGB", (width, 40 + line_height * len(lines)), bg)
    draw = ImageDraw.Draw(image)
    regions: list[dict] = []
    y = 20
    for line in lines:
        box = draw.textbbox((40, y), line, font=font)
        draw.text((40, y), line, font=font, fill=fg)
        regions.append({"text": line, "bbox": list(box)})
        y += line_height
    return image, {"expected_lines": lines, "regions": regions, "exact": True}


def generate(out: Path) -> dict:
    font_path = _font_path()
    font_sha256 = hashlib.sha256(font_path.read_bytes()).hexdigest()
    fixtures: dict[str, dict] = {}

    image, spec = _text_fixture(
        font_path,
        "canonical_clear",
        ["Report-v2.txt", "9.3.1", "MixedCase Label: Alpha-42", "12345!"],
        size=64,
        bg=(255, 255, 255),
        fg=(0, 0, 0),
    )
    _save(image, out / "canonical_clear.png")
    fixtures["canonical_clear.png"] = spec | {"purpose": "exact multiline contract"}

    image, spec = _text_fixture(
        font_path,
        "small_font_desktop",
        ["Desktop file: Report-v2.txt", "Release 9.3.1", "Mixed-case token Zeta-907"],
        size=40,
        bg=(245, 247, 250),
        fg=(15, 20, 30),
        width=1800,
    )
    _save(image, out / "small_font_desktop.png")
    fixtures["small_font_desktop.png"] = spec | {"purpose": "CER <= 2 percent"}

    image = Image.new("RGB", (1800, 430), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(font_path, 64)
    columns = [
        (60, ["LEFT", "L-01", "9.3.1"]),
        (1080, ["RIGHT", "R-02", "OK"]),
    ]
    regions = []
    for x, lines in columns:
        for row, line in enumerate(lines):
            y = 40 + row * 115
            box = draw.textbbox((x, y), line, font=font)
            draw.text((x, y), line, font=font, fill=(0, 0, 0))
            regions.append({"text": line, "bbox": list(box)})
    _save(image, out / "multi_column.png")
    fixtures["multi_column.png"] = {
        "expected_lines": ["LEFT", "L-01", "9.3.1", "RIGHT", "R-02", "OK"],
        "regions": regions,
        "exact": False,
        "purpose": "column reading order",
    }

    # RGBA input exercises deterministic alpha compositing onto white.
    image = Image.new("RGBA", (1600, 230), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = _font(font_path, 58)
    draw.rounded_rectangle((20, 15, 1580, 215), radius=20, fill=(240, 245, 255, 210))
    line = "Transparent Panel: Alpha-42"
    box = draw.textbbox((70, 70), line, font=font)
    draw.text((70, 70), line, font=font, fill=(10, 30, 90, 255))
    _save(image, out / "transparent_ui.png")
    fixtures["transparent_ui.png"] = {
        "expected_lines": [line], "regions": [{"text": line, "bbox": list(box)}], "exact": True,
        "purpose": "RGBA decoding and alpha compositing",
    }

    image, spec = _text_fixture(
        font_path, "dark_ui", ["ERROR 503", "Retry-After: 120"], size=72,
        bg=(0, 0, 0), fg=(255, 255, 255), width=1600,
    )
    _save(image, out / "dark_ui.png")
    fixtures["dark_ui.png"] = spec | {"purpose": "dark UI contrast"}

    image = Image.new("RGB", (1000, 240), (255, 255, 255))
    _save(image, out / "blank.png")
    fixtures["blank.png"] = {"expected_lines": [], "regions": [], "exact": True, "purpose": "no_text"}

    # A deliberately degraded fixture is evaluated for honest warning behavior,
    # not an exact transcript, because degradation is intentionally ambiguous.
    image, _ = _text_fixture(
        font_path, "degraded", ["Uncertain token: Q7-19"], size=32,
        bg=(205, 205, 205), fg=(150, 150, 150), width=1100,
    )
    image = image.resize((440, image.height // 2), Image.Resampling.BILINEAR)
    image = image.filter(__import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur(1.4))
    _save(image, out / "degraded.png")
    fixtures["degraded.png"] = {
        "expected_lines": ["Uncertain token: Q7-19"], "regions": [], "exact": False,
        "warnings_required": ["low_confidence", "partial_text"], "purpose": "uncertainty honesty",
    }

    manifest = {
        "generator": "scripts/generate_ocr_fixtures.py",
        "generator_version": 1,
        "platform": platform.platform(),
        "font": {"path": font_path.relative_to(ROOT).as_posix()
                 if font_path.is_relative_to(ROOT) else font_path.name,
                 "sha256": font_sha256,
                 "license": "DejaVu Sans; Bitstream Vera derivative; license text in docs/ocr-easyocr-qualification.md"},
        "fixtures": fixtures,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    manifest = generate(args.output)
    print(json.dumps({"output": str(args.output), "fixtures": sorted(manifest["fixtures"]),
                      "font_sha256": manifest["font"]["sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
