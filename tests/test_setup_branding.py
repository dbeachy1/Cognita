"""Regression checks for Cognita Setup artwork and Windows shell icon resources."""

from __future__ import annotations

import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "windows" / "setup" / "Cognita.iss"
ASSETS = ROOT / "windows" / "setup" / "assets"


def test_setup_uses_tall_wizard_art_and_custom_setup_icon():
    source = ISS.read_text(encoding="utf-8")
    assert "WizardImageFile=assets\\cognita-wizard.png" in source
    assert "SetupIconFile=assets\\cognita-setup.ico" in source

    artwork = (ASSETS / "cognita-wizard.png").read_bytes()
    assert artwork[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", artwork[16:24])
    assert width >= 512
    assert height >= width * 2


def test_setup_icon_contains_standard_windows_sizes_through_256():
    icon = (ASSETS / "cognita-setup.ico").read_bytes()
    reserved, icon_type, image_count = struct.unpack_from("<HHH", icon)
    assert (reserved, icon_type) == (0, 1)

    sizes = set()
    for index in range(image_count):
        width, height, _colors, _reserved, _planes, _bits, _length, _offset = struct.unpack_from(
            "<BBBBHHII", icon, 6 + index * 16
        )
        sizes.add((width or 256, height or 256))

    assert {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)} <= sizes
