"""The Windows installer's PowerShell helper (windows/CognitaWin.ps1), tested as design 14.2 says.

Each windows/tests/test_*.ps1 is a PowerShell test script. It dot-sources the helper with -NoMain,
replaces Invoke-External (the one function every external process goes through), the fake clock and
the small registry/CIM/file-system readers, and calls the helper's functions directly. So these tests
never touch the real WSL, registry, scheduled tasks, network or the user's profile, and none of them
waits on the wall clock. The few that run a REAL child process (the password path: a second
powershell.exe reading its stdin, a real named pipe pair) wait only on that process or pipe, never on
a sleep; the subprocess timeout below is a hang guard for a run that is GUARANTEED to finish.

This wrapper runs each script under Windows PowerShell 5.1 (the shell every Windows 11 has and the one
Setup uses), asserts exit code 0 and reports the script's own failure lines. It also checks the shipped
files' encoding (pure ASCII: 5.1 reads a BOM-less UTF-8 file as ANSI) and that they parse under 5.1.
Skipped where powershell.exe is absent (every non-Windows machine).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WINDOWS_DIR = REPO / "windows"
TEST_SCRIPTS = sorted(p for p in (WINDOWS_DIR / "tests").glob("test_*.ps1"))
SHIPPED = sorted(
    [
        WINDOWS_DIR / "CognitaWin.ps1",
        WINDOWS_DIR / "cognita.ps1",
        WINDOWS_DIR / "launch-keepalive.vbs",
        *sorted((WINDOWS_DIR / "tests").glob("*.ps1")),
    ]
)
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None,
    reason="Windows PowerShell 5.1 (powershell.exe) is not available",
)

# Hang guard only. The slowest script (the keepalive one, which starts a fake wsl a dozen times)
# finishes in well under a minute; this is generous so a loaded machine cannot fail it.
SCRIPT_HANG_GUARD_SECONDS = 900


def _run_ps(args: list[str]) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    # Same as the cognita.exe launcher: a PSModulePath inherited from PowerShell 7 makes Windows
    # PowerShell 5.1 load 7's module copies and fail; without it 5.1 uses its own default.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_HANG_GUARD_SECONDS,
        check=False,
        env=env,
    )


def test_there_are_test_scripts() -> None:
    names = {p.name for p in TEST_SCRIPTS}
    # The design's 14.2 helper list, one script per area; a missing file is a deleted test.
    for expected in (
        "test_core.ps1",
        "test_paths_fstab.ps1",
        "test_checks.ps1",
        "test_distro.ps1",
        "test_password.ps1",
        "test_install.ps1",
        "test_cli.ps1",
        "test_remote_uninstall_diag.ps1",
        "test_keepalive.ps1",
    ):
        assert expected in names, f"{expected} is missing from windows/tests"


@pytest.mark.parametrize("script", TEST_SCRIPTS, ids=lambda p: p.name)
def test_powershell_script(script: Path) -> None:
    result = _run_ps(["-File", str(script)])
    out = result.stdout + result.stderr
    failures = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    summary = [ln for ln in out.splitlines() if " passed, " in ln]
    assert result.returncode == 0, f"{script.name} exited {result.returncode}\n" + (
        "\n".join(failures) if failures else out[-4000:]
    )
    assert summary, f"{script.name} printed no summary line:\n{out[-2000:]}"
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_shipped_file_is_ascii_and_lf(path: Path) -> None:
    data = path.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 127]
    assert not bad, f"{path.name} has a non-ASCII byte at offset {bad[0]}"
    assert b"\r" not in data, f"{path.name} contains a carriage return (LF only)"


@pytest.mark.parametrize("path", [p for p in SHIPPED if p.suffix == ".ps1"], ids=lambda p: p.name)
def test_powershell_file_parses_under_5_1(path: Path) -> None:
    # The syntax check from the brief: nothing printed means no parse errors.
    command = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}',[ref]$null,[ref]$e); $e | ForEach-Object "
        "{ $_.Message + ' @ ' + $_.Extent.StartLineNumber }"
    )
    result = _run_ps(["-Command", command])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"parse errors in {path.name}: {result.stdout}"


def test_no_powershell_7_only_features_in_the_helper() -> None:
    """The 5.1 parse test above already rejects PS 7 syntax (??, ternary, &&); -Parallel is a switch."""
    lines = (WINDOWS_DIR / "CognitaWin.ps1").read_text(encoding="ascii").splitlines()
    code = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    assert "-Parallel" not in code


# \b keeps "keep" and "keepalive" from matching the host name.
BANNED = re.compile(r"\bdougb\b|\bDoug\b|\bMaia\b|\bkei\b|B:\\AI|E:\\VMs", re.IGNORECASE)


def test_no_personal_paths_or_names_in_windows_files() -> None:
    """C22: nothing under windows/ names a person, a private machine or a personal path."""
    for path in [*SHIPPED, WINDOWS_DIR / "README.md"]:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = BANNED.search(line)
            assert m is None, f"{path.name}:{i} contains {m.group(0)!r}: {line.strip()}"
