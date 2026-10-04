"""Compiles and runs the Pascal test harness for Cognita Setup's PURE block (design 19.10).

windows/setup/pure.iss holds the functions of Cognita.iss that need no wizard (mode choice, result-line
and JSON parsing, token redaction, PATH editing, the version compare, ...). windows/setup/tests/
pure_tests.iss #includes it and runs a list of cases when it starts, writing a PASS or FAIL line for each
plus a `TOTAL <n> FAILED <m>` line to the file named by /RESULT=. This test compiles that script with
ISCC (found the way windows/build_setup.py finds it), runs the result silently and asserts on the file.

The harness never runs the real Setup and touches nothing on the machine. It waits on the process
itself (subprocess.run's own wait), not on a clock; the timeout is only a hang guard so a wedged Setup
stub fails the test instead of freezing the run.

Skipped, with the reason, when ISCC is not installed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP_DIR = REPO / "windows" / "setup"
HARNESS = SETUP_DIR / "tests" / "pure_tests.iss"

_SPEC = importlib.util.spec_from_file_location("cognita_build_setup_for_pure", REPO / "windows" / "build_setup.py")
_BS = importlib.util.module_from_spec(_SPEC)
sys.modules["cognita_build_setup_for_pure"] = _BS
_SPEC.loader.exec_module(_BS)


def _find_iscc() -> Path | None:
    try:
        return _BS.find_iscc(os.environ)
    except _BS.BuildError:
        return None


ISCC = _find_iscc()

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Inno Setup builds Windows programs only"),
    pytest.mark.skipif(
        ISCC is None,
        reason="ISCC.exe (Inno Setup 6.3 or later) was not found; install it with the winget command in "
        "windows/build_setup.py to run the Setup harness",
    ),
]

HANG_GUARD_S = 300  # a hang guard for a wedged process, not a wait that a correct run depends on


@pytest.fixture(scope="module")
def harness_report(tmp_path_factory) -> tuple[list[str], str]:
    """Compile the harness, run it, and return (report lines, Setup's log text)."""
    out = tmp_path_factory.mktemp("setup-pure")
    compile_log = out / "iscc.txt"
    compiled = subprocess.run(
        [str(ISCC), "/Q", f"/O{out}", "/Fpure_tests", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD_S,
    )
    compile_log.write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
    assert compiled.returncode == 0, f"ISCC failed (exit {compiled.returncode}):\n{compiled.stdout}{compiled.stderr}"
    exe = out / "pure_tests.exe"
    assert exe.is_file(), f"ISCC succeeded but {exe} was not written"

    result_file = out / "result.txt"
    setup_log = out / "setup.log"
    ran = subprocess.run(
        [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={setup_log}", f"/RESULT={result_file}"],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD_S,
    )
    log_text = setup_log.read_text(encoding="utf-8", errors="replace") if setup_log.is_file() else ""
    assert result_file.is_file(), (
        f"the harness wrote no result file (exit {ran.returncode}); Setup's log:\n{log_text}"
    )
    lines = [ln for ln in result_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return lines, log_text


def test_pure_harness_has_no_failures(harness_report):
    lines, log_text = harness_report
    failures = [ln for ln in lines if ln.startswith("FAIL")]
    assert not failures, "Setup PURE cases failed:\n" + "\n".join(failures) + "\n\nSetup log:\n" + log_text


def test_pure_harness_ran_a_real_number_of_cases(harness_report):
    lines, _ = harness_report
    totals = [ln for ln in lines if ln.startswith("TOTAL ")]
    assert len(totals) == 1, f"expected exactly one TOTAL line, got {totals!r}"
    match = re.fullmatch(r"TOTAL (\d+) FAILED (\d+)", totals[0])
    assert match, f"unreadable TOTAL line: {totals[0]!r}"
    total, failed = int(match.group(1)), int(match.group(2))
    assert failed == 0, f"{failed} of {total} cases failed"
    assert total > 0, "the harness ran no cases"
    passes = [ln for ln in lines if ln.startswith("PASS ")]
    assert len(passes) == total, f"TOTAL says {total} cases but the file has {len(passes)} PASS lines"


@pytest.mark.parametrize(
    "case",
    [
        "PickMode fresh: no distro",
        "PickMode finish: distro, install never completed",
        "PickMode repair: same version",
        "PickMode update: older installed",
        "PickMode reinstall: keep-data uninstall, same version",
        "ModeFromState foreign distro is fresh",
        "PickMode downgrade is still an update (InitializeSetup refuses it)",
        "Decode %253B is the text %3B",
        "Decode trailing %",
        "ResultValue decodes ;",
        "JsonField escaped quote and newline",
        "RedactToken path",
        "PathRemovePart middle",
        "UrlInText sign-in message",
        "CompareVersions 14.10 is newer than 14.9",
        "DowngradeText",
        "ProofSkipVisible proof stage, not pressed",
        "ProofSkipVisible proof stage, pressed",
        "ProofSkipVisible other stage",
        "SkippedProofNote",
        "ResultValue proof=skipped parse",
        # Design 22.7 and 22.12: the Acceleration page's pure rules.
        "AccelPageMode update hides (ok, build)",
        "AccelPageMode old without a build is still the driver text",
        "AccelArg update, page hidden",
        "AccelArg fresh, page hidden is cpu (the user never chose a GPU)",
        "AccelArg repair, page hidden keeps",
        "AccelArg repair, unknown, untouched passes nothing",
        "AccelArg reinstall, unknown, untouched passes nothing",
        "AccelArg repair, unknown, user chose nvidia",
        "AccelReadyReason update has none (no card)",
        "AccelCognitaBytes unknown takes the larger (nvidia build)",
        "AccelFinishedLine the result did not say: no line",
        "WarningFix acceleration stage without a fix",
        "WslReclaimBoxVisible unset",
        "WslReclaimBoxVisible set",
    ],
)
def test_named_case_ran_and_passed(harness_report, case):
    """The cases the design names must be in the file: a harness that quietly dropped one would still
    show zero failures."""
    lines, _ = harness_report
    assert f"PASS {case}" in lines, f"case {case!r} did not run or did not pass"
