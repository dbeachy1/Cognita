"""Tests for windows/build_setup.py (docs/DESIGN-WINDOWS-INSTALLER.md section 11).

Every external command (csc, ISCC, gh) goes through the injectable runner, so these tests run no
compiler, touch no network, and use no clock: the fake runner records each argument list and writes
the files the real tool would have written.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("cognita_build_setup", REPO / "windows" / "build_setup.py")
bs = importlib.util.module_from_spec(_SPEC)
sys.modules["cognita_build_setup"] = bs
_SPEC.loader.exec_module(bs)

IMAGE_BYTES = b"pretend linux image"
SRC_BYTES = b"pretend source tree"
IMAGE_SHA = hashlib.sha256(IMAGE_BYTES).hexdigest()
SRC_SHA = hashlib.sha256(SRC_BYTES).hexdigest()

RELEASE_TEXT = f"""version: 14.1.0
commit: 0123456789abcdef0123456789abcdef01234567
published_at: 2026-09-29T09:00:00-07:00
image_ref_cognita_cpu: ghcr.io/example/cognita-app@sha256:{"a" * 64}
image_ref_workspace_runtime: ghcr.io/example/cognita-workspace-runtime@sha256:{"b" * 64}
image_ref_toolbox: ghcr.io/example/cognita-workspace-toolbox@sha256:{"c" * 64}
toolbox_version: 12.6.0
size_cognita_cpu: 1500000000
size_workspace_runtime: 1400000000
size_toolbox: 500000000
wsl_image_sha256: {IMAGE_SHA}
size_wsl_image: {len(IMAGE_BYTES)}
src_tarball_sha256: {SRC_SHA}
size_src_tarball: {len(SRC_BYTES)}
"""


class FakeRunner:
    """Stands in for csc.exe, ISCC.exe, signtool.exe and gh."""

    def __init__(self, gh_files: dict[str, bytes] | None = None):
        self.calls: list[list[str]] = []
        self.gh_files = gh_files if gh_files is not None else {}
        self.exit_codes: dict[str, int] = {}
        self.fail_signtool_action: str | None = None
        self.fail_signtool_target: str | None = None

    def __call__(self, argv, *, cwd=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = Path(argv[0]).name.lower()
        code = self.exit_codes.get(tool, 0)
        if (tool == "signtool.exe" and self.fail_signtool_action == argv[1]
                and (self.fail_signtool_target is None or Path(argv[-1]).name == self.fail_signtool_target)):
            code = 1
        if code != 0:
            return bs.RunResult(code, "", f"{tool} failed on purpose")
        if tool == "csc.exe":
            out = next(a for a in argv if a.startswith("/out:"))[len("/out:"):]
            Path(out).write_bytes(b"MZ launcher")
        elif tool == "iscc.exe":
            defines = {a[2:].split("=", 1)[0]: a.split("=", 1)[1] for a in argv if a.startswith("/D")}
            exe = Path(defines["OutputDir"]) / f"Cognita-Setup-{defines['Version']}-r{defines['Revision']}.exe"
            exe.write_bytes(b"MZ setup " + defines["Revision"].encode())
            if any(arg.startswith("/Sazurecodesign=") for arg in argv):
                exe.write_bytes(exe.read_bytes() + b" signed by Inno")
        elif tool == "signtool.exe" and argv[1] == "sign":
            target = Path(argv[-1])
            target.write_bytes(target.read_bytes() + b" signed")
        elif tool == "gh" and argv[1:3] == ["release", "download"]:
            target = Path(argv[argv.index("--dir") + 1])
            for index, arg in enumerate(argv):
                if arg == "--pattern":
                    name = argv[index + 1]
                    (target / name).write_bytes(self.gh_files[name])
        return bs.RunResult(0, "", "")

    def calls_of(self, tool: str) -> list[list[str]]:
        return [c for c in self.calls if Path(c[0]).name.lower() == tool]


@pytest.fixture
def workspace(tmp_path):
    """A fake repo, a fake Windows folder holding csc.exe and ISCC.exe, and a release file."""
    repo = tmp_path / "repo"
    (repo / "windows" / "setup").mkdir(parents=True)
    (repo / "windows" / "launcher").mkdir(parents=True)
    (repo / "windows" / "locales").mkdir(parents=True)
    (repo / "windows" / "setup" / "Cognita.iss").write_text("; iss", encoding="utf-8")
    locale_entry = {
        "setup.progress_titles": {"fixture_stage": "Fixture stage"},
        "wsl.warning": {"title": "Title", "message": "Message", "fix": "Fix"},
    }
    for locale in bs.SETUP_LOCALES:
        (repo / "windows" / "locales" / f"windows-setup.{locale}.json").write_text(
            json.dumps(locale_entry), encoding="utf-8"
        )
    iss_lines = ["[Languages]"]
    for language in bs.SETUP_LANGUAGE_IDS:
        iss_lines.append(f'Name: "{language}"; MessagesFile: "stock.isl"')
    iss_lines.append("[CustomMessages]")
    for language in bs.SETUP_LANGUAGE_IDS:
        iss_lines.append(f"{language}.example=Example")
    for locale in bs.SETUP_LOCALES:
        name = f"windows-setup.{locale}.json"
        iss_lines.extend((name, name))
    (repo / "windows" / "setup" / "Cognita.iss").write_text("\n".join(iss_lines), encoding="utf-8")
    (repo / "windows" / "launcher" / "cognita.cs").write_text("// cs", encoding="utf-8")
    icon = repo / "src" / "cognita" / "web" / "cognita-icon-512.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"PNG fixture")
    csc = tmp_path / "windir" / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    csc.parent.mkdir(parents=True)
    csc.write_bytes(b"")
    iscc = tmp_path / "localapp" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    iscc.parent.mkdir(parents=True)
    iscc.write_bytes(b"")
    release = tmp_path / "published-release.txt"
    release.write_text(RELEASE_TEXT, encoding="utf-8")
    image = tmp_path / "local-image.tar.gz"
    image.write_bytes(IMAGE_BYTES)
    src = tmp_path / "local-src.tar.gz"
    src.write_bytes(SRC_BYTES)
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({
        "Endpoint": "https://example.codesigning.azure.net/",
        "CodeSigningAccountName": "fixture-account",
        "CertificateProfileName": "fixture-profile",
    }), encoding="utf-8")
    signtool = tmp_path / "Windows SDK" / "signtool.exe"
    signtool.parent.mkdir()
    signtool.write_bytes(b"")
    dlib = tmp_path / "Artifact Signing" / "Azure.CodeSigning.Dlib.dll"
    dlib.parent.mkdir()
    dlib.write_bytes(b"")
    return {
        "repo": repo, "csc": csc, "iscc": iscc, "release": release, "image": image, "src": src,
        "metadata": metadata, "signtool": signtool, "dlib": dlib,
        "dist": tmp_path / "dist", "env": {"WINDIR": str(tmp_path / "windir"), "LOCALAPPDATA": str(tmp_path / "localapp")},
    }


def test_setup_catalog_validator_requires_all_six_and_matching_message_ids(workspace):
    windows_dir = workspace["repo"] / "windows"
    iss = windows_dir / "setup" / "Cognita.iss"
    bs.validate_setup_catalogs(windows_dir, iss)

    (windows_dir / "locales" / "windows-setup.fr-FR.json").unlink()
    with pytest.raises(bs.BuildError, match="catalog is missing"):
        bs.validate_setup_catalogs(windows_dir, iss)


def test_setup_catalog_validator_requires_matching_progress_titles_in_all_locales(workspace):
    windows_dir = workspace["repo"] / "windows"
    iss = windows_dir / "setup" / "Cognita.iss"
    french = windows_dir / "locales" / "windows-setup.fr-FR.json"
    data = json.loads(french.read_text(encoding="utf-8"))
    del data["setup.progress_titles"]
    french.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bs.BuildError, match="different message ID set"):
        bs.validate_setup_catalogs(windows_dir, iss)


def test_setup_catalog_validator_rejects_placeholder_drift(workspace):
    windows_dir = workspace["repo"] / "windows"
    iss = windows_dir / "setup" / "Cognita.iss"
    english = {
        "setup.progress_titles": {"fixture_stage": "Fixture stage"},
        "wsl.warning": {"title": "Title", "message": "Code {exit_code}", "fix": "Fix"},
    }
    translated = {
        "setup.progress_titles": {"fixture_stage": "Etapa"},
        "wsl.warning": {"title": "Título", "message": "Código", "fix": "Solución"},
    }
    (windows_dir / "locales" / "windows-setup.en-US.json").write_text(json.dumps(english), encoding="utf-8")
    for locale in bs.SETUP_LOCALES[1:]:
        data = translated if locale == "es-ES" else english
        (windows_dir / "locales" / f"windows-setup.{locale}.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bs.BuildError, match="placeholder set"):
        bs.validate_setup_catalogs(windows_dir, iss)


def make_options(ws, **overrides):
    values = {"published_release": ws["release"], "dist_dir": ws["dist"], "image": ws["image"], "src": ws["src"]}
    values.update(overrides)
    return bs.Options(**values)


def run_build(ws, runner, **overrides):
    logs: list[str] = []
    exe = bs.build(make_options(ws, **overrides), runner=runner, env=ws["env"], log=logs.append, repo_root=ws["repo"])
    return exe, logs


# --- published-release.txt -------------------------------------------------------------------


def test_parse_published_release_keeps_colons_in_values_and_skips_comments():
    text = "# comment\n\nversion: 14.1.0\nimage_ref_cognita_cpu: ghcr.io/x/y@sha256:abc\n  size_toolbox :  5 \n"
    parsed = bs.parse_published_release(text)
    assert parsed == {
        "version": "14.1.0",
        "image_ref_cognita_cpu": "ghcr.io/x/y@sha256:abc",
        "size_toolbox": "5",
    }


def test_parse_published_release_rejects_a_line_without_a_colon():
    with pytest.raises(bs.BuildError, match="line 2"):
        bs.parse_published_release("version: 1\nnot a pair\n")


def test_read_published_release_reports_missing_required_keys(tmp_path):
    path = tmp_path / "published-release.txt"
    path.write_text("version: 14.1.0\nsize_cognita_cpu: 1\n", encoding="utf-8")
    with pytest.raises(bs.BuildError) as error:
        bs.read_published_release(path)
    assert "size_workspace_runtime" in str(error.value)
    assert "size_toolbox" in str(error.value)


def test_read_published_release_missing_file_says_where_it_comes_from(tmp_path):
    with pytest.raises(bs.BuildError, match="release.py publish"):
        bs.read_published_release(tmp_path / "nope.txt")


# --- ISCC and csc discovery ------------------------------------------------------------------


def test_missing_iscc_prints_the_winget_command(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "a"), "ProgramFiles": str(tmp_path / "b")}
    with pytest.raises(bs.BuildError) as error:
        bs.find_iscc(env, is_file=lambda path: False, which=lambda name: None)
    message = str(error.value)
    assert "winget install --id JRSoftware.InnoSetup -e --scope user" in message
    assert "ISCC.exe" in message


def test_find_iscc_prefers_the_per_user_install_then_program_files_then_path(tmp_path):
    env = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "ProgramFiles(x86)": str(tmp_path / "pf86"),
        "ProgramFiles": str(tmp_path / "pf"),
    }
    per_user = tmp_path / "local" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    pf86 = tmp_path / "pf86" / "Inno Setup 6" / "ISCC.exe"
    assert bs.find_iscc(env, is_file=lambda p: p in (per_user, pf86), which=lambda n: None) == per_user
    assert bs.find_iscc(env, is_file=lambda p: p == pf86, which=lambda n: None) == pf86
    assert bs.find_iscc(env, is_file=lambda p: False, which=lambda n: "C:\\tools\\ISCC.exe") == Path("C:\\tools\\ISCC.exe")


def test_missing_csc_is_reported(tmp_path):
    with pytest.raises(bs.BuildError, match="C# compiler is missing"):
        bs.find_csc({"WINDIR": str(tmp_path)}, is_file=lambda path: False)


# --- payload verification --------------------------------------------------------------------


def test_downloaded_tarball_with_the_wrong_hash_is_refused_and_nothing_is_compiled(workspace):
    runner = FakeRunner(gh_files={"cognita-wsl-14.1.0.tar.gz": b"tampered bytes", "cognita-src-14.1.0.tar.gz": SRC_BYTES})
    with pytest.raises(bs.BuildError, match="SHA-256 mismatch"):
        run_build(workspace, runner, image=None, src=None)
    assert runner.calls_of("iscc.exe") == []
    assert runner.calls_of("csc.exe") == []


def test_local_tarball_that_disagrees_with_the_recorded_hash_is_refused(workspace):
    workspace["image"].write_bytes(b"some other file")
    runner = FakeRunner()
    with pytest.raises(bs.BuildError, match="published-release.txt records"):
        run_build(workspace, runner)
    assert runner.calls == []


def test_local_tarball_with_no_recorded_hash_uses_its_own_hash_and_says_so(workspace):
    stripped = "\n".join(line for line in RELEASE_TEXT.splitlines() if not line.startswith(("wsl_image_sha256", "src_tarball_sha256")))
    workspace["release"].write_text(stripped + "\n", encoding="utf-8")
    runner = FakeRunner()
    _, logs = run_build(workspace, runner)
    defines = runner.calls_of("iscc.exe")[0]
    assert f"/DImageSha256={IMAGE_SHA}" in defines
    assert f"/DSrcSha256={SRC_SHA}" in defines
    assert any("WARNING" in line and "wsl_image_sha256" in line for line in logs)


def test_download_without_a_recorded_hash_is_refused(workspace):
    stripped = "\n".join(line for line in RELEASE_TEXT.splitlines() if not line.startswith("wsl_image_sha256"))
    workspace["release"].write_text(stripped + "\n", encoding="utf-8")
    with pytest.raises(bs.BuildError, match="wsl_image_sha256"):
        run_build(workspace, FakeRunner(), image=None)


def test_download_runs_gh_release_download_and_accepts_matching_hashes(workspace):
    runner = FakeRunner(gh_files={"cognita-wsl-14.1.0.tar.gz": IMAGE_BYTES, "cognita-src-14.1.0.tar.gz": SRC_BYTES})
    exe, _ = run_build(workspace, runner, image=None, src=None, repo="example/cognita")
    build_dir = workspace["dist"] / "build-setup"
    assert runner.calls_of("gh") == [
        ["gh", "release", "download", "v14.1.0", "--pattern", "cognita-wsl-14.1.0.tar.gz", "--dir", str(build_dir),
         "--clobber", "--repo", "example/cognita"],
        ["gh", "release", "download", "v14.1.0", "--pattern", "cognita-src-14.1.0.tar.gz", "--dir", str(build_dir),
         "--clobber", "--repo", "example/cognita"],
    ]
    assert exe.exists()


def test_an_already_downloaded_matching_tarball_is_not_downloaded_again(workspace):
    build_dir = workspace["dist"] / "build-setup"
    build_dir.mkdir(parents=True)
    (build_dir / "cognita-wsl-14.1.0.tar.gz").write_bytes(IMAGE_BYTES)
    (build_dir / "cognita-src-14.1.0.tar.gz").write_bytes(SRC_BYTES)
    runner = FakeRunner()
    run_build(workspace, runner, image=None, src=None)
    assert runner.calls_of("gh") == []


def test_a_failed_download_is_reported_with_its_error(workspace):
    runner = FakeRunner()
    runner.exit_codes["gh"] = 1
    with pytest.raises(bs.BuildError, match="gh release download failed"):
        run_build(workspace, runner, image=None, src=None)


# --- the ISCC command line and the output ----------------------------------------------------


def test_the_exact_iscc_argument_list(workspace):
    runner = FakeRunner()
    run_build(workspace, runner, revision=2)
    build_dir = workspace["dist"] / "build-setup"
    windows_dir = workspace["repo"] / "windows"
    assert runner.calls_of("iscc.exe") == [[
        str(workspace["iscc"]),
        "/Q",
        "/DVersion=14.1.0",
        "/DRevision=2",
        f"/DImagePath={workspace['image']}",
        f"/DImageSha256={IMAGE_SHA}",
        f"/DImageSize={len(IMAGE_BYTES)}",
        f"/DSrcPath={workspace['src']}",
        f"/DSrcSha256={SRC_SHA}",
        f"/DSrcSize={len(SRC_BYTES)}",
        "/DSizeCognitaCpu=1500000000",
        "/DSizeCognitaNvidia=0",           # RELEASE_TEXT has no NVIDIA build
        "/DSizeWorkspaceRuntime=1400000000",
        "/DSizeToolbox=500000000",
        f"/DLauncherExe={build_dir / 'cognita.exe'}",
        f"/DWindowsDir={windows_dir}",
        f"/DOutputDir={workspace['dist']}",
        str(windows_dir / "setup" / "Cognita.iss"),
    ]]


NVIDIA_LINES = (
    f"image_ref_cognita_nvidia: ghcr.io/example/cognita-app@sha256:{'d' * 64}\n"
    "size_cognita_nvidia: 5100000000\n"
)


def _define_value(runner, name):
    [call] = runner.calls_of("iscc.exe")
    [value] = [arg.split("=", 1)[1] for arg in call if arg.startswith(f"/D{name}=")]
    return value


def test_the_nvidia_size_is_passed_when_the_release_has_the_image_and_the_size(workspace):
    workspace["release"].write_text(RELEASE_TEXT + NVIDIA_LINES, encoding="utf-8")
    runner = FakeRunner()
    _, logs = run_build(workspace, runner)
    assert _define_value(runner, "SizeCognitaNvidia") == "5100000000"
    assert any(line.startswith("NVIDIA build: size_cognita_nvidia=5100000000") for line in logs)
    # The define sits right after SizeCognitaCpu, before the workspace sizes.
    [call] = runner.calls_of("iscc.exe")
    names = [arg.split("=", 1)[0] for arg in call if arg.startswith("/D")]
    assert names.index("/DSizeCognitaNvidia") == names.index("/DSizeCognitaCpu") + 1


def test_the_nvidia_size_is_zero_and_logged_when_the_release_has_no_nvidia_build(workspace):
    runner = FakeRunner()
    _, logs = run_build(workspace, runner)
    assert _define_value(runner, "SizeCognitaNvidia") == "0"
    assert any("NVIDIA build: not in this release" in line for line in logs)


def test_the_nvidia_size_is_zero_when_only_one_of_the_two_keys_is_present(workspace):
    only_size = RELEASE_TEXT + "size_cognita_nvidia: 5100000000\n"
    only_image = RELEASE_TEXT + f"image_ref_cognita_nvidia: ghcr.io/example/cognita-app@sha256:{'d' * 64}\n"
    for text, expected_log in ((only_size, "image_ref_cognita_nvidia absent, size_cognita_nvidia present"),
                               (only_image, "image_ref_cognita_nvidia present, size_cognita_nvidia absent")):
        workspace["release"].write_text(text, encoding="utf-8")
        runner = FakeRunner()
        _, logs = run_build(workspace, runner)
        assert _define_value(runner, "SizeCognitaNvidia") == "0"
        assert any("NVIDIA build: not in this release" in line and expected_log in line for line in logs)


def test_a_nvidia_size_that_is_not_a_number_stops_the_build(workspace):
    workspace["release"].write_text(
        RELEASE_TEXT + NVIDIA_LINES.replace("5100000000", "five gigabytes"), encoding="utf-8")
    with pytest.raises(bs.BuildError, match="size_cognita_nvidia is not a whole number"):
        run_build(workspace, FakeRunner())


def test_the_launcher_is_compiled_with_the_in_box_csc(workspace):
    runner = FakeRunner()
    run_build(workspace, runner)
    build_dir = workspace["dist"] / "build-setup"
    assert runner.calls_of("csc.exe") == [[
        str(workspace["csc"]), "/nologo", "/target:exe", "/optimize+", f"/out:{build_dir / 'cognita.exe'}",
        str(workspace["repo"] / "windows" / "launcher" / "cognita.cs"),
    ]]
    csc_index = runner.calls.index(runner.calls_of("csc.exe")[0])
    iscc_index = runner.calls.index(runner.calls_of("iscc.exe")[0])
    assert csc_index < iscc_index


def test_output_is_named_with_the_revision_and_gets_a_sha256_file(workspace):
    runner = FakeRunner()
    exe, _ = run_build(workspace, runner, revision=3)
    assert exe == workspace["dist"] / "Cognita-Setup-14.1.0-r3.exe"
    sha_file = workspace["dist"] / "Cognita-Setup-14.1.0-r3.exe.sha256"
    expected = hashlib.sha256(exe.read_bytes()).hexdigest()
    assert sha_file.read_bytes() == f"{expected}  Cognita-Setup-14.1.0-r3.exe\n".encode("ascii")


def test_the_default_revision_is_one(workspace):
    exe, _ = run_build(workspace, FakeRunner())
    assert exe.name == "Cognita-Setup-14.1.0-r1.exe"


def test_a_revision_below_one_is_refused(workspace):
    with pytest.raises(bs.BuildError, match="--revision"):
        run_build(workspace, FakeRunner(), revision=0)


def test_iscc_failure_is_reported_and_no_hash_file_is_written(workspace):
    runner = FakeRunner()
    runner.exit_codes["iscc.exe"] = 2
    with pytest.raises(bs.BuildError, match="ISCC failed"):
        run_build(workspace, runner)
    assert not list(workspace["dist"].glob("*.sha256"))


def test_csc_failure_stops_before_iscc(workspace):
    runner = FakeRunner()
    runner.exit_codes["csc.exe"] = 1
    with pytest.raises(bs.BuildError, match="csc failed"):
        run_build(workspace, runner)
    assert runner.calls_of("iscc.exe") == []


def test_missing_iscc_stops_the_build_with_the_winget_hint(workspace):
    workspace["iscc"].unlink()
    with pytest.raises(bs.BuildError, match="winget install --id JRSoftware.InnoSetup"):
        bs.build(make_options(workspace), runner=FakeRunner(), env={**workspace["env"], "ProgramFiles": "", "ProgramFiles(x86)": ""},
                 log=lambda line: None, repo_root=workspace["repo"])


def test_a_double_quote_in_a_path_is_refused_not_passed_to_iscc():
    with pytest.raises(bs.BuildError, match="double quote"):
        bs._define_text("ImagePath", 'C:\\bad"path')


# --- upload and signing ----------------------------------------------------------------------


def signing_options(ws):
    return {
        "signing_metadata": ws["metadata"], "signtool": ws["signtool"], "signing_dlib": ws["dlib"],
    }


def test_upload_attaches_the_setup_and_its_hash_to_the_release(workspace):
    runner = FakeRunner()
    exe, _ = run_build(workspace, runner, upload=True, **signing_options(workspace))
    sha_file = exe.with_name(exe.name + ".sha256")
    assert runner.calls_of("gh") == [
        ["gh", "release", "upload", "v14.1.0", str(exe), str(sha_file), "--clobber"],
    ]


def test_nothing_is_uploaded_without_the_flag(workspace):
    runner = FakeRunner()
    _, logs = run_build(workspace, runner)
    assert runner.calls_of("gh") == []
    assert any("upload: skipped" in line for line in logs)


def test_unsigned_local_build_remains_available(workspace):
    _, logs = run_build(workspace, FakeRunner())
    assert any("signing: skipped (no signing configuration)" in line for line in logs)


def test_upload_requires_signing_configuration_before_building(workspace):
    runner = FakeRunner()
    with pytest.raises(bs.BuildError, match="--upload requires"):
        run_build(workspace, runner, upload=True)
    assert runner.calls == []


def test_signed_build_signs_and_verifies_launcher_before_iscc(workspace):
    runner = FakeRunner()
    run_build(workspace, runner, **signing_options(workspace))
    launcher_sign = next(i for i, call in enumerate(runner.calls) if Path(call[0]).name == "signtool.exe" and call[1] == "sign")
    launcher_verify = next(i for i, call in enumerate(runner.calls) if Path(call[0]).name == "signtool.exe" and call[1] == "verify")
    iscc_index = next(i for i, call in enumerate(runner.calls) if Path(call[0]).name == "ISCC.exe")
    assert launcher_sign < launcher_verify < iscc_index
    sign_call = runner.calls[launcher_sign]
    assert sign_call[1:] == [
        "sign", "/fd", "SHA256", "/tr", bs.SIGNING_TIMESTAMP_URL, "/td", "SHA256",
        "/dlib", str(workspace["dlib"]), "/dmdf", str(workspace["metadata"]),
        str(workspace["dist"] / "build-setup" / "cognita.exe"),
    ]
    iscc_call = runner.calls[iscc_index]
    assert "/DSigningEnabled=1" in iscc_call
    assert any(arg.startswith("/Sazurecodesign=") and "$q" in arg and "$f" in arg for arg in iscc_call)


def test_inno_signing_is_enabled_only_for_signed_compiles():
    iss = (REPO / "windows" / "setup" / "Cognita.iss").read_text(encoding="utf-8")
    assert "#ifdef SigningEnabled\nSignTool=azurecodesign\nSignedUninstaller=yes\nSignToolRunMinimized=yes\n#endif" in iss


def test_signing_paths_can_come_from_environment(workspace):
    runner = FakeRunner()
    env = {
        **workspace["env"],
        bs.SIGNING_METADATA_ENV: str(workspace["metadata"]),
        bs.SIGNTOOL_ENV: str(workspace["signtool"]),
        bs.SIGNING_DLIB_ENV: str(workspace["dlib"]),
    }
    bs.build(make_options(workspace), runner=runner, env=env, log=lambda line: None, repo_root=workspace["repo"])
    assert any(Path(call[0]).name == "signtool.exe" and call[1] == "sign" for call in runner.calls)


def test_relative_paths_are_absolute_by_the_time_iscc_receives_them(workspace, monkeypatch):
    monkeypatch.chdir(workspace["repo"].parent)
    runner = FakeRunner()
    options = make_options(
        workspace,
        image=Path("local-image.tar.gz"), src=Path("local-src.tar.gz"),
        dist_dir=Path("relative-dist"), build_dir=Path("relative-dist/build-setup"),
        signing_metadata=Path("metadata.json"), signtool=Path("Windows SDK/signtool.exe"),
        signing_dlib=Path("Artifact Signing/Azure.CodeSigning.Dlib.dll"),
    )
    exe = bs.build(options, runner=runner, env=workspace["env"], log=lambda line: None, repo_root=workspace["repo"])
    [iscc_call] = runner.calls_of("iscc.exe")
    defines = {arg[2:].split("=", 1)[0]: arg.split("=", 1)[1] for arg in iscc_call if arg.startswith("/D")}
    assert defines["ImagePath"] == str(workspace["image"].resolve())
    assert defines["SrcPath"] == str(workspace["src"].resolve())
    assert defines["OutputDir"] == str((workspace["repo"].parent / "relative-dist").resolve())
    assert str(workspace["dlib"].resolve()) in next(arg for arg in iscc_call if arg.startswith("/Sazurecodesign="))
    assert exe.is_absolute()


def test_setup_is_verified_after_inno_signing_before_hash_and_upload(workspace):
    runner = FakeRunner()
    exe, _ = run_build(workspace, runner, upload=True, **signing_options(workspace))
    calls = runner.calls
    setup_verify = next(i for i, call in enumerate(calls) if Path(call[0]).name == "signtool.exe" and call[1] == "verify" and call[-1] == str(exe))
    upload = next(i for i, call in enumerate(calls) if Path(call[0]).name == "gh")
    assert calls[setup_verify][1:5] == ["verify", "/pa", "/all", "/tw"]
    assert setup_verify < upload
    expected = hashlib.sha256(exe.read_bytes()).hexdigest()
    sha_file = exe.with_name(exe.name + ".sha256")
    assert sha_file.read_bytes() == f"{expected}  {exe.name}\n".encode("ascii")


def test_signing_failure_stops_before_iscc_and_hashing(workspace):
    runner = FakeRunner()
    runner.fail_signtool_action = "sign"
    with pytest.raises(bs.BuildError, match="SignTool failed"):
        run_build(workspace, runner, **signing_options(workspace))
    assert runner.calls_of("iscc.exe") == []
    assert not list(workspace["dist"].glob("*.sha256"))


def test_signature_verification_failure_stops_before_hashing(workspace):
    runner = FakeRunner()
    runner.fail_signtool_action = "verify"
    with pytest.raises(bs.BuildError, match="SignTool verification failed"):
        run_build(workspace, runner, **signing_options(workspace))
    assert runner.calls_of("iscc.exe") == []
    assert not list(workspace["dist"].glob("*.sha256"))


def test_setup_verification_failure_stops_before_hash_and_upload(workspace):
    runner = FakeRunner()
    runner.fail_signtool_action = "verify"
    runner.fail_signtool_target = "Cognita-Setup-14.1.0-r1.exe"
    old_sha = workspace["dist"] / "Cognita-Setup-14.1.0-r1.exe.sha256"
    old_sha.parent.mkdir(parents=True)
    old_sha.write_text("stale hash", encoding="ascii")
    with pytest.raises(bs.BuildError, match="SignTool verification failed"):
        run_build(workspace, runner, upload=True, **signing_options(workspace))
    assert runner.calls_of("gh") == []
    assert not old_sha.exists()


def test_incomplete_signing_configuration_fails_closed(workspace):
    with pytest.raises(bs.BuildError, match="missing --signtool, --signing-dlib"):
        run_build(workspace, FakeRunner(), signing_metadata=workspace["metadata"])


def test_every_log_line_carries_a_local_timestamp(capsys):
    import datetime

    fixed = datetime.datetime(2026, 9, 29, 9, 30, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=-7)))
    bs.make_logger(now=lambda: fixed)("hello")
    assert capsys.readouterr().out == "2026-09-29 09:30:15-0700 build_setup: hello\n"
