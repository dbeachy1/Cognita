#!/usr/bin/env python3
"""Build Cognita-Setup-<version>-r<N>.exe on the Windows build machine (docs/DESIGN-WINDOWS-INSTALLER.md section 11).

What it does, in order (every step is logged with the local time):

1. Reads ``containers/published-release.txt`` (the file ``release.py publish`` writes on kei).
2. Gets the two payload tarballs, ``cognita-wsl-<version>.tar.gz`` (the Linux image) and
   ``cognita-src-<version>.tar.gz`` (the source tree for updates): either the local files named by
   ``--image`` and ``--src`` (for testing before any GitHub release exists), or a download from the
   GitHub release for that version with ``gh release download``. A downloaded file must match the
   SHA-256 recorded in ``published-release.txt``; a mismatch stops the build. A local file is also
   refused if the file records a different hash for it.
3. Finds ``ISCC.exe`` (Inno Setup 6.3 or later) and the in-box C# compiler.
4. Compiles ``windows/launcher/cognita.cs`` to ``cognita.exe`` in the build folder and, for a
   signed build, signs and verifies it.
5. Runs ISCC on ``windows/setup/Cognita.iss`` with the version, revision, hashes, download sizes
   (SizeCognitaCpu, SizeCognitaNvidia, SizeWorkspaceRuntime, SizeToolbox) and file paths as ``/D``
   defines. ``SizeCognitaNvidia`` is ``size_cognita_nvidia`` when published-release.txt lists both it
   and ``image_ref_cognita_nvidia``, else 0 ("this Setup has no NVIDIA build"; design 22.7). Writes
   ``dist/Cognita-Setup-<version>-r<N>.exe``.
6. Verifies the signed Setup and writes its SHA-256 after signing.
7. ``--upload`` attaches both files to the same GitHub release with ``gh release upload``.

``setup_revision`` (``--revision``, default 1) starts at 1 per version and moves on any change to
``windows/`` or the Setup, so two Setup builds of one version can be told apart. A Setup build changes
no shipped app code, so it needs no app version bump.

Python standard library only. Every external command (csc, ISCC, signtool, gh) goes through one
injectable runner so the tests can fake them.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_DIR = REPO_ROOT / "windows"
DEFAULT_PUBLISHED_RELEASE = REPO_ROOT / "containers" / "published-release.txt"
ISS_PATH = WINDOWS_DIR / "setup" / "Cognita.iss"
LAUNCHER_SOURCE = WINDOWS_DIR / "launcher" / "cognita.cs"
DEFAULT_DIST = REPO_ROOT / "dist"

WINGET_HINT = "winget install --id JRSoftware.InnoSetup -e --scope user"
CSC_RELATIVE = Path("Microsoft.NET") / "Framework64" / "v4.0.30319" / "csc.exe"

# Keys ``release.py publish`` writes into published-release.txt (Linux design 5.5 and 19.3).
REQUIRED_KEYS = ("version", "size_cognita_cpu", "size_workspace_runtime", "size_toolbox")
# The two keys the WSL image recipe appends (Linux design 19.3, Windows design section 6).
IMAGE_SHA_KEY = "wsl_image_sha256"
IMAGE_SIZE_KEY = "size_wsl_image"
# The source tarball's keys are not named in either design yet; they are constants here so the
# publish side and this file agree in one place.
SRC_SHA_KEY = "src_tarball_sha256"
SRC_SIZE_KEY = "size_src_tarball"
SETUP_LOCALES = ("en-US", "es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR")
SETUP_LANGUAGE_IDS = ("english", "spanish", "french", "german", "italian", "brazilianportuguese")


class BuildError(Exception):
    """A build step failed; the message is what the operator sees (no traceback)."""


@dataclasses.dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., RunResult]
Logger = Callable[[str], None]


def subprocess_runner(argv: Sequence[str], *, cwd: Path | None = None) -> RunResult:
    """The real runner: argv as a list (never a command string), output captured as UTF-8."""
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return RunResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def make_logger(now: Callable[[], datetime.datetime] | None = None) -> Logger:
    """A logger that stamps each line with the machine's local time (house rule)."""
    clock = now or (lambda: datetime.datetime.now().astimezone())

    def log(message: str) -> None:
        print(f"{clock().strftime('%Y-%m-%d %H:%M:%S%z')} build_setup: {message}", flush=True)

    return log


@contextlib.contextmanager
def timed(log: Logger, label: str) -> Iterator[None]:
    start = time.perf_counter()
    log(f"{label}: start")
    try:
        yield
    finally:
        log(f"{label}: finished in {int((time.perf_counter() - start) * 1000)} ms")


# --- published-release.txt -------------------------------------------------------------------


def parse_published_release(text: str) -> dict[str, str]:
    """``key: value`` lines. Blank lines and ``#`` comments are skipped. Values keep their colons
    (an image reference is ``ghcr.io/x/y@sha256:...``), so only the first colon splits."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            raise BuildError(f"published-release.txt line {number} is not 'key: value': {raw!r}")
        values[key.strip()] = value.strip()
    return values


def read_published_release(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BuildError(
            f"{path} does not exist. It is written by 'release.py publish' on kei and committed; "
            "pull it, or pass --published-release PATH."
        )
    values = parse_published_release(path.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise BuildError(f"{path} is missing required keys: {', '.join(missing)}")
    return values


def _int_field(values: Mapping[str, str], key: str) -> int:
    raw = values.get(key, "")
    try:
        number = int(raw)
    except ValueError:
        raise BuildError(f"published-release.txt: {key} is not a whole number of bytes: {raw!r}") from None
    if number < 0:
        raise BuildError(f"published-release.txt: {key} is negative: {number}")
    return number


# --- tool discovery --------------------------------------------------------------------------


def find_iscc(env: Mapping[str, str], is_file: Callable[[Path], bool] | None = None,
              which: Callable[[str], str | None] | None = None) -> Path:
    """Inno Setup's compiler: per-user install, then Program Files, then PATH."""
    exists = is_file or Path.is_file
    lookup = which or shutil.which
    candidates: list[Path] = []
    for var, sub in (
        ("LOCALAPPDATA", Path("Programs") / "Inno Setup 6"),
        ("ProgramFiles(x86)", Path("Inno Setup 6")),
        ("ProgramFiles", Path("Inno Setup 6")),
    ):
        base = env.get(var)
        if base:
            candidates.append(Path(base) / sub / "ISCC.exe")
    for candidate in candidates:
        if exists(candidate):
            return candidate
    on_path = lookup("ISCC.exe")
    if on_path:
        return Path(on_path)
    raise BuildError(
        "Inno Setup 6.3 or later was not found (ISCC.exe). Install it for this user with:\n"
        f"    {WINGET_HINT}\n"
        "Searched: " + ", ".join(str(c) for c in candidates) + " and PATH."
    )


def find_csc(env: Mapping[str, str], is_file: Callable[[Path], bool] | None = None) -> Path:
    """The C# compiler that ships with .NET Framework 4.x on every Windows (no SDK needed)."""
    exists = is_file or Path.is_file
    root = env.get("WINDIR") or env.get("SystemRoot")
    if not root:
        raise BuildError("WINDIR is not set, so the in-box C# compiler cannot be located.")
    csc = Path(root) / CSC_RELATIVE
    if not exists(csc):
        raise BuildError(f"The in-box C# compiler is missing: {csc}")
    return csc


# --- payload tarballs ------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclasses.dataclass(frozen=True)
class Payload:
    """One payload tarball, ready for the .iss: where it is, its SHA-256 and its size in bytes."""

    path: Path
    sha256: str
    size: int


def resolve_payload(
    *,
    label: str,
    asset_name: str,
    sha_key: str,
    size_key: str,
    values: Mapping[str, str],
    local_path: Path | None,
    build_dir: Path,
    tag: str,
    repo: str | None,
    runner: Runner,
    log: Logger,
) -> Payload:
    """Use the local file, or the release download, and prove it against published-release.txt."""
    recorded_sha = values.get(sha_key, "").lower()
    if local_path is not None:
        local_path = local_path.resolve()
        if not local_path.is_file():
            raise BuildError(f"--{label} {local_path} is not a file")
        actual = sha256_file(local_path)
        size = local_path.stat().st_size
        if recorded_sha and recorded_sha != actual:
            raise BuildError(
                f"{label}: SHA-256 of {local_path} is {actual}, but published-release.txt records "
                f"{recorded_sha} ({sha_key}). Refusing to build with a file that is not the published one."
            )
        if not recorded_sha:
            log(
                f"{label}: WARNING published-release.txt has no {sha_key}; using the local file's own "
                f"hash {actual} (a test build, not a release build)"
            )
        log(f"{label}: local file {local_path} size={size} sha256={actual} decision=use")
        return Payload(local_path, actual, size)

    if not recorded_sha:
        raise BuildError(
            f"published-release.txt has no {sha_key}, so {asset_name} cannot be verified. "
            f"Finish 'release.py publish' first, or pass --{label} PATH to build with a local file."
        )
    target = build_dir / asset_name
    if target.is_file() and sha256_file(target) == recorded_sha:
        log(f"{label}: {target} already downloaded and matches {sha_key}; decision=reuse")
    else:
        argv = ["gh", "release", "download", tag, "--pattern", asset_name, "--dir", str(build_dir), "--clobber"]
        if repo:
            argv += ["--repo", repo]
        log(f"{label}: downloading {asset_name} from release {tag}: {' '.join(argv)}")
        with timed(log, f"download {asset_name}"):
            result = runner(argv)
        if result.returncode != 0:
            raise BuildError(f"gh release download failed (exit {result.returncode}) for {asset_name}:\n"
                             f"{(result.stderr or result.stdout).strip()}")
        if not target.is_file():
            raise BuildError(f"gh release download reported success but {target} does not exist")
    actual = sha256_file(target)
    if actual != recorded_sha:
        raise BuildError(
            f"{label}: SHA-256 mismatch for {target}: got {actual}, published-release.txt records "
            f"{recorded_sha}. The download is not the published file; refusing to build."
        )
    size = target.stat().st_size
    recorded_size = values.get(size_key)
    if recorded_size and _int_field(values, size_key) != size:
        raise BuildError(
            f"{label}: {target} is {size} bytes but published-release.txt records {recorded_size} ({size_key})"
        )
    log(f"{label}: verified {target} size={size} sha256={actual}")
    return Payload(target, actual, size)


# --- ISCC defines ----------------------------------------------------------------------------


def _define_text(name: str, value: str) -> str:
    # Checked on ISCC 6.7.3: a command-line /D value is always taken as plain text (a hash that
    # looks like a float, "0123e45...", stays text), so no quoting goes INSIDE the argument. A value
    # with spaces is quoted as a whole argument by subprocess ("/DName=a b"), which ISCC accepts;
    # quotes inside the value (the Windows escape \") are NOT understood by ISCC, so refuse them.
    if '"' in value:
        raise BuildError(f"cannot pass {name} to ISCC: value contains a double quote: {value!r}")
    return f"/D{name}={value}"


def _define_number(name: str, value: int) -> str:
    return f"/D{name}={int(value)}"


def nvidia_build_size(values: Mapping[str, str]) -> int:
    """15.1.0 (DESIGN-WINDOWS-INSTALLER 22.7 "Build input"): the NVIDIA image's download size when this release
    has an NVIDIA build (BOTH ``image_ref_cognita_nvidia`` and ``size_cognita_nvidia`` in
    published-release.txt), else 0, which makes Setup offer no NVIDIA choice. Never raises for an absent key;
    a present size that is not a whole number is a build error like the other sizes."""
    if values.get("image_ref_cognita_nvidia") and values.get("size_cognita_nvidia"):
        return _int_field(values, "size_cognita_nvidia")
    return 0


def describe_nvidia_build(values: Mapping[str, str]) -> str:
    """The log line for the decision above, with the values it was made from."""
    size = nvidia_build_size(values)
    if size:
        return (f"NVIDIA build: size_cognita_nvidia={size} "
                f"image_ref_cognita_nvidia={values['image_ref_cognita_nvidia']}")
    image_state = "present" if values.get("image_ref_cognita_nvidia") else "absent"
    size_state = "present" if values.get("size_cognita_nvidia") else "absent"
    return ("NVIDIA build: not in this release "
            f"(image_ref_cognita_nvidia {image_state}, size_cognita_nvidia {size_state}); SizeCognitaNvidia=0")


def validate_setup_catalogs(windows_dir: Path, iss_path: Path) -> None:
    """Fail before compiling if the helper's temporary and installed locale payload is incomplete."""
    catalog_dir = windows_dir / "locales"
    catalogs: dict[str, dict[str, object]] = {}
    for locale in SETUP_LOCALES:
        path = catalog_dir / f"windows-setup.{locale}.json"
        if not path.is_file():
            raise BuildError(f"Windows Setup locale catalog is missing: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"Windows Setup locale catalog is invalid: {path}: {exc}") from None
        if not isinstance(data, dict) or not data:
            raise BuildError(f"Windows Setup locale catalog must be a nonempty JSON object: {path}")
        for message_id, entry in data.items():
            if message_id == "setup.progress_titles":
                if not isinstance(entry, dict) or not entry or any(
                    not isinstance(title, str) or not title for title in entry.values()
                ):
                    raise BuildError(f"{path}: setup.progress_titles must be a nonempty map of title strings")
                continue
            if not isinstance(entry, dict) or not all(isinstance(entry.get(field), str) and entry[field]
                                                       for field in ("title", "message", "fix")):
                raise BuildError(f"{path}: {message_id!r} needs nonempty title, message, and fix text")
        catalogs[locale] = data
    english_keys = set(catalogs["en-US"])
    if "setup.progress_titles" not in english_keys:
        raise BuildError("Windows Setup catalogs must define setup.progress_titles for every supported stage")
    def placeholders(value: str) -> set[str]:
        return set(re.findall(r"\{([A-Za-z][A-Za-z0-9_]*)\}", value))

    for locale, data in catalogs.items():
        if set(data) != english_keys:
            raise BuildError(f"Windows Setup catalog {locale} has a different message ID set from en-US")
        base_titles = catalogs["en-US"]["setup.progress_titles"]
        translated_titles = data["setup.progress_titles"]
        if set(translated_titles) != set(base_titles):
            raise BuildError(f"Windows Setup catalog {locale} has a different progress stage title set from en-US")
        for stage, title in base_titles.items():
            if placeholders(str(translated_titles[stage])) != placeholders(str(title)):
                raise BuildError(f"Windows Setup catalog {locale} has a different placeholder set for progress title {stage}")
        for message_id in sorted(english_keys):
            if message_id == "setup.progress_titles":
                continue
            base = catalogs["en-US"][message_id]
            translated = data[message_id]
            for field in ("title", "message", "fix"):
                if placeholders(str(translated[field])) != placeholders(str(base[field])):
                    raise BuildError(f"Windows Setup catalog {locale} has a different placeholder set for {message_id}.{field}")
            base_reasons = base.get("reasons", {})
            reasons = translated.get("reasons", {})
            if set(reasons) != set(base_reasons):
                raise BuildError(f"Windows Setup catalog {locale} has a different reason set for {message_id}")
            for reason_id, phrase in base_reasons.items():
                if placeholders(str(reasons[reason_id])) != placeholders(str(phrase)):
                    raise BuildError(f"Windows Setup catalog {locale} has a different placeholder set for {message_id}.reasons.{reason_id}")
    iss = iss_path.read_text(encoding="utf-8")
    languages_section = re.search(r"(?ims)^\[Languages\]\s*(.*?)(?=^\[|\Z)", iss)
    if not languages_section:
        raise BuildError(f"{iss_path} is missing its [Languages] section")
    language_ids = set(re.findall(r'(?im)^Name:\s*"([^"]+)"', languages_section.group(1)))
    if language_ids != set(SETUP_LANGUAGE_IDS):
        raise BuildError(f"{iss_path} must declare exactly the six supported Setup languages")
    custom_section = re.search(r"(?ims)^\[CustomMessages\]\s*(.*?)(?=^\[|\Z)", iss)
    if not custom_section:
        raise BuildError(f"{iss_path} is missing its [CustomMessages] section")
    custom_keys = {language: set() for language in SETUP_LANGUAGE_IDS}
    for language, key in re.findall(r"(?im)^([A-Za-z]+)\.([A-Za-z0-9_]+)=", custom_section.group(1)):
        if language in custom_keys:
            custom_keys[language].add(key)
    english_custom_keys = custom_keys["english"]
    if not english_custom_keys or any(keys != english_custom_keys for keys in custom_keys.values()):
        raise BuildError(f"{iss_path} must define the same nonempty [CustomMessages] key set for all six languages")
    for locale in SETUP_LOCALES:
        name = f"windows-setup.{locale}.json"
        if iss.count(name) < 2:
            raise BuildError(f"{iss_path} must package {name} for both temporary and installed helper use")


def iscc_defines(
    *,
    version: str,
    revision: int,
    image: Payload,
    src: Payload,
    values: Mapping[str, str],
    launcher_exe: Path,
    windows_dir: Path,
    dist_dir: Path,
) -> list[str]:
    """The exact ``/D`` argument list, in a fixed order (the tests assert it)."""
    return [
        _define_text("Version", version),
        _define_number("Revision", revision),
        _define_text("ImagePath", str(image.path)),
        _define_text("ImageSha256", image.sha256),
        _define_number("ImageSize", image.size),
        _define_text("SrcPath", str(src.path)),
        _define_text("SrcSha256", src.sha256),
        _define_number("SrcSize", src.size),
        _define_number("SizeCognitaCpu", _int_field(values, "size_cognita_cpu")),
        _define_number("SizeCognitaNvidia", nvidia_build_size(values)),
        _define_number("SizeWorkspaceRuntime", _int_field(values, "size_workspace_runtime")),
        _define_number("SizeToolbox", _int_field(values, "size_toolbox")),
        _define_text("LauncherExe", str(launcher_exe)),
        _define_text("WindowsDir", str(windows_dir)),
        _define_text("OutputDir", str(dist_dir)),
    ]


def setup_exe_name(version: str, revision: int) -> str:
    return f"Cognita-Setup-{version}-r{revision}.exe"


# --- signing ---------------------------------------------------------------------------------

SIGNING_METADATA_ENV = "COGNITA_SIGNING_METADATA"
SIGNTOOL_ENV = "COGNITA_SIGNTOOL"
SIGNING_DLIB_ENV = "COGNITA_SIGNING_DLIB"
SIGNING_TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"


@dataclasses.dataclass(frozen=True)
class SigningConfig:
    metadata: Path
    signtool: Path
    dlib: Path


def resolve_signing_config(options: "Options", env: Mapping[str, str]) -> SigningConfig | None:
    """Resolve an optional, complete Microsoft Artifact Signing configuration."""
    values = (
        options.signing_metadata or (Path(env[SIGNING_METADATA_ENV]) if env.get(SIGNING_METADATA_ENV) else None),
        options.signtool or (Path(env[SIGNTOOL_ENV]) if env.get(SIGNTOOL_ENV) else None),
        options.signing_dlib or (Path(env[SIGNING_DLIB_ENV]) if env.get(SIGNING_DLIB_ENV) else None),
    )
    if not any(values):
        if options.upload:
            raise BuildError("--upload requires --signing-metadata, --signtool, and --signing-dlib (or their environment variables)")
        return None
    if not all(values):
        missing = [name for name, value in zip(("--signing-metadata", "--signtool", "--signing-dlib"), values) if not value]
        raise BuildError(f"signed builds require all signing paths; missing {', '.join(missing)}")
    metadata, signtool, dlib = values
    assert metadata is not None and signtool is not None and dlib is not None
    metadata, signtool, dlib = metadata.resolve(), signtool.resolve(), dlib.resolve()
    for label, path in (("signing metadata", metadata), ("SignTool", signtool), ("signing dlib", dlib)):
        if not path.is_file():
            raise BuildError(f"{label} file does not exist: {path}")
    try:
        metadata_values = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read signing metadata {metadata}: {error}") from None
    required = ("Endpoint", "CodeSigningAccountName", "CertificateProfileName")
    if not isinstance(metadata_values, dict) or any(not isinstance(metadata_values.get(key), str) or not metadata_values[key].strip() for key in required):
        raise BuildError("signing metadata must be a JSON object with Endpoint, CodeSigningAccountName, and CertificateProfileName")
    return SigningConfig(metadata, signtool, dlib)


def sign_file(path: Path, config: SigningConfig, runner: Runner, log: Logger) -> None:
    argv = [
        str(config.signtool), "sign", "/fd", "SHA256", "/tr", SIGNING_TIMESTAMP_URL, "/td", "SHA256",
        "/dlib", str(config.dlib), "/dmdf", str(config.metadata), str(path),
    ]
    log(f"sign: {path.name}")
    with timed(log, f"sign {path.name}"):
        result = runner(argv)
    if result.returncode != 0:
        raise BuildError(f"SignTool failed (exit {result.returncode}) signing {path.name}:\n{(result.stdout + result.stderr).strip()}")


def verify_signature(path: Path, config: SigningConfig, runner: Runner, log: Logger) -> None:
    argv = [str(config.signtool), "verify", "/pa", "/all", "/tw", str(path)]
    log(f"verify signature: {path.name}")
    with timed(log, f"verify signature {path.name}"):
        result = runner(argv)
    if result.returncode != 0:
        raise BuildError(f"SignTool verification failed (exit {result.returncode}) for {path.name}:\n{(result.stdout + result.stderr).strip()}")


def inno_signtool(config: SigningConfig) -> str:
    """The command ISCC passes to its configured SignTool; Inno expands $q and $f."""
    quote = "$q"
    return (
        f"{quote}{config.signtool}{quote} sign /fd SHA256 /tr {SIGNING_TIMESTAMP_URL} /td SHA256 "
        f"/dlib {quote}{config.dlib}{quote} /dmdf {quote}{config.metadata}{quote} $f"
    )


# --- the build -------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Options:
    revision: int = 1
    image: Path | None = None
    src: Path | None = None
    published_release: Path = DEFAULT_PUBLISHED_RELEASE
    dist_dir: Path = DEFAULT_DIST
    build_dir: Path | None = None
    tag: str | None = None
    repo: str | None = None
    upload: bool = False
    signing_metadata: Path | None = None
    signtool: Path | None = None
    signing_dlib: Path | None = None


def build(
    options: Options,
    *,
    runner: Runner = subprocess_runner,
    env: Mapping[str, str] | None = None,
    log: Logger | None = None,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Run the whole build and return the path of the Setup executable."""
    env = os.environ if env is None else env
    log = log or make_logger()
    if options.revision < 1:
        raise BuildError(f"--revision must be 1 or more, got {options.revision}")
    signing = resolve_signing_config(options, env)

    windows_dir = repo_root / "windows"
    iss = windows_dir / "setup" / "Cognita.iss"
    launcher_source = windows_dir / "launcher" / "cognita.cs"
    icon_asset = repo_root / "src" / "cognita" / "web" / "cognita-icon-512.png"
    for needed in (iss, launcher_source, icon_asset):
        if not needed.is_file():
            raise BuildError(f"{needed} does not exist")
    validate_setup_catalogs(windows_dir, iss)

    values = read_published_release(options.published_release)
    version = values["version"]
    tag = options.tag or f"v{version}"
    dist_dir = options.dist_dir.resolve()
    build_dir = options.build_dir.resolve() if options.build_dir else (dist_dir / "build-setup")
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"build start: version={version} revision={options.revision} tag={tag} "
        f"release_file={options.published_release} build_dir={build_dir} dist={dist_dir} "
        f"image={'local ' + str(options.image) if options.image else 'download'} "
        f"src={'local ' + str(options.src) if options.src else 'download'} upload={options.upload}"
    )

    image = resolve_payload(
        label="image", asset_name=f"cognita-wsl-{version}.tar.gz", sha_key=IMAGE_SHA_KEY,
        size_key=IMAGE_SIZE_KEY, values=values, local_path=options.image, build_dir=build_dir,
        tag=tag, repo=options.repo, runner=runner, log=log,
    )
    src = resolve_payload(
        label="src", asset_name=f"cognita-src-{version}.tar.gz", sha_key=SRC_SHA_KEY,
        size_key=SRC_SIZE_KEY, values=values, local_path=options.src, build_dir=build_dir,
        tag=tag, repo=options.repo, runner=runner, log=log,
    )

    iscc = find_iscc(env)
    csc = find_csc(env)
    log(f"tools: iscc={iscc} csc={csc}" + (f" signtool={signing.signtool} dlib={signing.dlib}" if signing else ""))

    launcher_exe = build_dir / "cognita.exe"
    launcher_exe.unlink(missing_ok=True)
    csc_argv = [str(csc), "/nologo", "/target:exe", "/optimize+", f"/out:{launcher_exe}", str(launcher_source)]
    log(f"launcher: {' '.join(csc_argv)}")
    with timed(log, "compile launcher"):
        result = runner(csc_argv)
    if result.returncode != 0 or not launcher_exe.is_file():
        raise BuildError(
            f"csc failed (exit {result.returncode}) compiling {launcher_source}:\n"
            f"{(result.stdout + result.stderr).strip()}"
        )
    log(f"launcher: built {launcher_exe} size={launcher_exe.stat().st_size}")
    if signing:
        sign_file(launcher_exe, signing, runner, log)
        verify_signature(launcher_exe, signing, runner, log)

    log(describe_nvidia_build(values))
    defines = iscc_defines(
        version=version, revision=options.revision, image=image, src=src, values=values,
        launcher_exe=launcher_exe, windows_dir=windows_dir, dist_dir=dist_dir,
    )
    exe = dist_dir / setup_exe_name(version, options.revision)
    sha_file = exe.with_name(exe.name + ".sha256")
    exe.unlink(missing_ok=True)
    sha_file.unlink(missing_ok=True)
    iscc_argv = [str(iscc), "/Q", *defines, str(iss)]
    if signing:
        iscc_argv[-1:-1] = ["/DSigningEnabled=1", f"/Sazurecodesign={inno_signtool(signing)}"]
    log(f"iscc: {' '.join(iscc_argv)}")
    with timed(log, "compile Setup"):
        result = runner(iscc_argv)
    if result.returncode != 0:
        raise BuildError(
            f"ISCC failed (exit {result.returncode}):\n{(result.stdout + result.stderr).strip()}"
        )
    if not exe.is_file():
        raise BuildError(f"ISCC reported success but {exe} was not written")

    if signing:
        verify_signature(exe, signing, runner, log)
    else:
        log(f"signing: skipped (no signing configuration); {exe.name} is unsigned")
    digest = sha256_file(exe)
    sha_file.write_bytes(f"{digest}  {exe.name}\n".encode("ascii"))
    log(f"built {exe} size={exe.stat().st_size} sha256={digest}; wrote {sha_file.name}")

    if options.upload:
        upload_argv = ["gh", "release", "upload", tag, str(exe), str(sha_file), "--clobber"]
        if options.repo:
            upload_argv += ["--repo", options.repo]
        log(f"upload: {' '.join(upload_argv)}")
        with timed(log, "upload"):
            result = runner(upload_argv)
        if result.returncode != 0:
            raise BuildError(
                f"gh release upload failed (exit {result.returncode}):\n"
                f"{(result.stderr or result.stdout).strip()}"
            )
        log(f"upload: {exe.name} and {sha_file.name} attached to release {tag}")
    else:
        log("upload: skipped (no --upload)")
    return exe


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description="Build Cognita-Setup-<version>-r<N>.exe.")
    parser.add_argument("--revision", type=int, default=1, help="setup_revision for this version (default 1)")
    parser.add_argument("--image", type=Path, help="local cognita-wsl tarball (skips the release download)")
    parser.add_argument("--src", type=Path, help="local cognita-src tarball (skips the release download)")
    parser.add_argument("--published-release", type=Path, default=DEFAULT_PUBLISHED_RELEASE,
                        help="path of published-release.txt (default containers/published-release.txt)")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST, help="output folder (default dist/)")
    parser.add_argument("--build-dir", type=Path, help="scratch folder (default <dist>/build-setup)")
    parser.add_argument("--tag", help="GitHub release tag (default v<version>)")
    parser.add_argument("--repo", help="OWNER/NAME for gh (default: gh's own detection)")
    parser.add_argument("--upload", action="store_true", help="attach the Setup and its .sha256 to the release")
    parser.add_argument("--signing-metadata", type=Path, help=f"Azure Artifact Signing metadata JSON (or {SIGNING_METADATA_ENV})")
    parser.add_argument("--signtool", type=Path, help=f"Windows SDK signtool.exe (or {SIGNTOOL_ENV})")
    parser.add_argument("--signing-dlib", type=Path, help=f"Azure.CodeSigning.Dlib.dll (or {SIGNING_DLIB_ENV})")
    ns = parser.parse_args(argv)
    return Options(
        revision=ns.revision, image=ns.image, src=ns.src, published_release=ns.published_release,
        dist_dir=ns.dist, build_dir=ns.build_dir, tag=ns.tag, repo=ns.repo, upload=ns.upload,
        signing_metadata=ns.signing_metadata, signtool=ns.signtool, signing_dlib=ns.signing_dlib,
    )


def main(argv: Sequence[str] | None = None) -> int:
    log = make_logger()
    try:
        options = parse_args(argv)
        exe = build(options, log=log)
    except BuildError as error:
        log(f"FAILED: {error}")
        return 1
    log(f"done: {exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
