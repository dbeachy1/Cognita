"""Coverage checks for the six-language Inno Setup message catalog."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "windows" / "setup" / "Cognita.iss"
LANGUAGES = (
    "english",
    "spanish",
    "french",
    "german",
    "italian",
    "brazilianportuguese",
)


def test_setup_custom_message_ids_match_across_all_supported_languages():
    source = ISS.read_text(encoding="utf-8")
    block = source.split("[CustomMessages]", 1)[1].split("\n[Files]", 1)[0]
    entries = re.findall(r"^(\w+)\.(\w+)=(.*)$", block, re.MULTILINE)
    by_language: dict[str, dict[str, str]] = {language: {} for language in LANGUAGES}
    for language, key, value in entries:
        if language in by_language:
            by_language[language][key] = value

    expected = set(by_language["english"])
    assert expected
    for language in LANGUAGES[1:]:
        assert set(by_language[language]) == expected, f"CustomMessages differ for {language}"

    referenced = set(re.findall(r"CustomMessage(?:WithLines)?\('([^']+)'\)", source))
    assert referenced <= expected, f"missing English CustomMessages: {sorted(referenced - expected)}"

    # These workflows were the review gaps: they must resolve through the selected catalog.
    for key in (
        "wslPageDescription",
        "verifyPasswordCaption",
        "busyCheckingThisPc",
        "busySavingPlace",
        "remoteProgressCaption",
        "remoteProgressPort",
        "progressElapsed",
        "progressBytesOf",
        "progressPreparingLinux",
        "failurePowerShellStart",
        "failureUnexpected",
        "failedInstallHeading",
        "progressInstalling",
        "warningAccelerationFix",
    ):
        assert key in expected


def test_reachable_setup_progress_and_failure_literals_use_catalog_messages():
    source = ISS.read_text(encoding="utf-8")
    for literal in (
        "'Checking this PC'",
        "'Turning on WSL'",
        "'Restarting Windows'",
        "'Saving your place'",
        "'Checking ports and disk space'",
        "'Setting up remote access'",
        "'Preparing'",
        "'Unpacking. This takes a moment.'",
        "'Windows PowerShell could not be started",
    ):
        assert literal not in source
    assert "UninstallProgressForm.StatusLabel.Caption := DisplayTitle;" in source
    assert "DisplayTitle := JsonField(L, 'title_display');" in source
