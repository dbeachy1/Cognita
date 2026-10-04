"""Fail-closed validation for release dependency lock inputs.

The release builder may consume these locks but this module never resolves or
downloads a dependency.  A lock is accepted only when every requirement is an
exact version pin with at least one SHA-256 artifact hash.  Missing locks and
unhashable historical drafts are deliberately reported as pending inputs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit, unquote


HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?\s*(?:(?:==\s*(?P<version>[^\s;]+))|(?:@\s*(?P<direct>\S+)))"
    r"(?P<rest>.*)$"
)
_HASH = re.compile(r"--hash=sha256:(?P<hash>[0-9a-f]{64})(?:\s|$)")
# 15.0.0 (DESIGN-NVIDIA-ACCELERATION 5.6): the NVIDIA image's two isolated runtimes are lock roles like
# the AMD ones. Both resolve on the CPU image's Python 3.13 base from PyPI alone.
LOCK_ROLES = ("service", "broker", "embedding", "ocr", "build", "test", "embedding-nvidia", "ocr-nvidia")
LOCK_FILES = {
    "service": "service-requirements.lock",
    "broker": "broker-requirements.lock",
    "embedding": "embedding-requirements.lock",
    "ocr": "ocr-requirements.lock",
    "ocr-cpu": "ocr-cpu-requirements.lock",
    "embedding-nvidia": "embedding-nvidia-requirements.lock",
    "ocr-nvidia": "ocr-nvidia-requirements.lock",
    "build": "build-requirements.lock",
    "test": "dev-requirements.lock",
}


class DependencyLockError(ValueError):
    """Raised when a release dependency input cannot prove reproducibility."""


@dataclasses.dataclass(frozen=True)
class LockedRequirement:
    name: str
    version: str
    hashes: tuple[str, ...]
    line: int

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "hashes": list(self.hashes), "line": self.line}


@dataclasses.dataclass(frozen=True)
class DependencyLockSet:
    roles: Mapping[str, tuple[LockedRequirement, ...]]
    source_paths: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "roles": {role: [item.as_dict() for item in values] for role, values in self.roles.items()},
            "source_paths": dict(self.source_paths),
        }


# The embedding environment has one deliberately narrow metadata exception:
# FastEmbed declares ``onnxruntime`` while the MIGraphX distribution owns that
# import namespace.  It does not waive hashes, version pins, provider checks,
# or any other pip-check discrepancy.
FASTEMBED_MIGRAPHX_EXCEPTION = {
    "declared_distribution": "onnxruntime",
    "installed_distribution": "onnxruntime-migraphx",
    "import_namespace": "onnxruntime",
    "provider": "MIGraphXExecutionProvider",
}


def _safe_lock_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if path.parent != root.resolve() or path.is_symlink() or not path.is_file():
        raise DependencyLockError(f"missing or linked dependency lock: {name}")
    return path


def parse_hash_locked_requirements(text: str, *, role: str = "lock") -> tuple[LockedRequirement, ...]:
    """Parse a requirements lock and require exact pins plus SHA-256 hashes."""
    records: list[LockedRequirement] = []
    seen: set[str] = set()
    continuation = False
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\") or line.startswith(("-r ", "--")):
            raise DependencyLockError(f"{role}:{line_number}: lock options/includes are not accepted")
        match = _PIN.fullmatch(line)
        if not match:
            raise DependencyLockError(f"{role}:{line_number}: requirement is not an exact version pin")
        direct = match.group("direct")
        if direct:
            parsed_url = urlsplit(direct)
            fragment = parsed_url.fragment
            fragment_digest = fragment.removeprefix("sha256=") if fragment.startswith("sha256=") else ""
            if (
                parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or not HEX64.fullmatch(fragment_digest)
            ):
                raise DependencyLockError(f"{role}:{line_number}: direct artifact URL must be HTTPS with a SHA-256 fragment")
        rest = match.group("rest")
        hashes = tuple(dict.fromkeys(_HASH.findall(rest)))
        if not hashes:
            raise DependencyLockError(f"{role}:{line_number}: requirement has no SHA-256 artifact hash")
        if any(not HEX64.fullmatch(value) for value in hashes):
            raise DependencyLockError(f"{role}:{line_number}: malformed artifact hash")
        if direct and fragment_digest not in hashes:
            raise DependencyLockError(f"{role}:{line_number}: direct artifact URL hash disagrees with requirement hashes")
        name = match.group("name").lower().replace("_", "-")
        if name in seen:
            raise DependencyLockError(f"{role}:{line_number}: duplicate package {name}")
        seen.add(name)
        version = match.group("version") or f"@ {direct}"
        records.append(LockedRequirement(name, version, hashes, line_number))
        continuation = False
    if continuation:
        raise DependencyLockError(f"{role}: dangling line continuation")
    if not records:
        raise DependencyLockError(f"{role}: lock is empty")
    return tuple(records)


def load_dependency_locks(root: Path | str, *, roles: Sequence[str] = LOCK_ROLES) -> DependencyLockSet:
    """Load the complete role set; missing/incomplete roles fail closed."""
    base = (Path(root).resolve() / "containers").resolve()
    parsed: dict[str, tuple[LockedRequirement, ...]] = {}
    paths: dict[str, str] = {}
    for role in roles:
        if role not in LOCK_FILES:
            raise DependencyLockError(f"unknown lock role: {role}")
        path = _safe_lock_path(base, LOCK_FILES[role])
        try:
            parsed[role] = parse_hash_locked_requirements(path.read_text(encoding="utf-8"), role=role)
        except OSError as exc:
            raise DependencyLockError(f"{role}: lock is unreadable") from exc
        paths[role] = path.relative_to(Path(root).resolve()).as_posix()
    for role, records in parsed.items():
        validate_lock_role(records, role=role)
    return DependencyLockSet(parsed, paths)


def validate_lock_role(records: Sequence[LockedRequirement], *, role: str) -> None:
    """Apply role-specific compatibility checks after syntax/hash validation."""
    if role == "ocr-cpu":
        names = {item.name for item in records}
        if not {"easyocr", "torch", "torchvision"} <= names:
            raise DependencyLockError("ocr-cpu: incomplete OCR runtime")
        if any(name.startswith(("nvidia-", "triton", "rocm", "onnxruntime")) for name in names):
            raise DependencyLockError("ocr-cpu: accelerator packages are forbidden")
        for item in records:
            if item.name in {"torch", "torchvision"} and (not item.version.startswith("@ https://download.pytorch.org/whl/cpu/") or "%2Bcpu-" not in item.version):
                raise DependencyLockError("ocr-cpu: Torch artifacts must be explicit CPU wheels")
        return
    if role == "embedding-nvidia":
        names = {item.name for item in records}
        if "fastembed" not in names or "onnxruntime-gpu" not in names:
            raise DependencyLockError("embedding-nvidia: FastEmbed/onnxruntime-gpu exception requires both pinned distributions")
        # onnxruntime-gpu owns the `onnxruntime` import namespace here exactly as onnxruntime-migraphx
        # does on AMD: FastEmbed's CPU onnxruntime is omitted from the lock, and neither the CPU wheel nor
        # the AMD provider may appear beside it.
        if "onnxruntime" in names or "onnxruntime-migraphx" in names:
            raise DependencyLockError("embedding-nvidia: CPU onnxruntime and onnxruntime-migraphx are forbidden beside onnxruntime-gpu")
        return
    if role == "ocr-nvidia":
        names = {item.name for item in records}
        if not {"easyocr", "torch", "torchvision"} <= names:
            raise DependencyLockError("ocr-nvidia: incomplete OCR runtime")
        # triton is deliberately not shipped (DESIGN-NVIDIA-ACCELERATION 5.4): its wheel bundles CUDA
        # developer binaries (ptxas, cuobjdump, nvdisasm) that the CUDA EULA does not list as
        # redistributable, and EasyOCR inference never compiles a kernel.
        if any(name.startswith(("triton", "rocm", "onnxruntime")) for name in names):
            raise DependencyLockError("ocr-nvidia: triton, ROCm and onnxruntime packages are forbidden")
        return
    if role != "embedding":
        return
    names = {item.name for item in records}
    if "fastembed" not in names or "onnxruntime-migraphx" not in names:
        raise DependencyLockError("embedding: FastEmbed/MIGraphX exception requires both pinned distributions")
    if "onnxruntime" in names:
        raise DependencyLockError("embedding: CPU onnxruntime is forbidden beside MIGraphX")


def cpu_ocr_declaration(path: Path | str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Read the single CPU authority, deriving vendor facts from wheel URLs.

    Resolution still verifies embedded wheel metadata and complete byte hashes;
    this boundary rejects unpinned inputs and an incompatible target beforehand.
    """
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("target") != {"python": "3.13", "abi": "cp313", "platform": "manylinux_2_28_x86_64"}:
        raise DependencyLockError("ocr-cpu: incompatible interpreter/platform declaration")
    versions: dict[str, str] = {}
    vendors: dict[str, dict[str, str]] = {}
    requirements = value.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise DependencyLockError("ocr-cpu: missing requirements declaration")
    for requirement in requirements:
        match = _PIN.fullmatch(str(requirement))
        if not match or match.group("rest"):
            raise DependencyLockError("ocr-cpu: declaration must contain exact pins or hashed wheel URLs")
        name = match.group("name").lower().replace("_", "-")
        if name in versions:
            raise DependencyLockError(f"ocr-cpu: duplicate declaration: {name}")
        direct = match.group("direct")
        if direct:
            url = urlsplit(direct)
            digest = url.fragment.removeprefix("sha256=")
            filename = unquote(url.path.rsplit("/", 1)[-1])
            fields = filename.removesuffix(".whl").split("-")
            if (url.scheme != "https" or url.netloc != "download.pytorch.org" or url.username
                    or url.password or url.query or not url.path.startswith("/whl/cpu/")
                    or not url.fragment.startswith("sha256=") or not HEX64.fullmatch(digest)
                    or len(fields) != 5 or fields[0] != name or name not in {"torch", "torchvision"}
                    or not fields[1].endswith("+cpu") or fields[2:] != ["cp313", "cp313", value["target"]["platform"]]):
                raise DependencyLockError(f"ocr-cpu: invalid vendor artifact: {name}")
            versions[name] = fields[1]
            vendors[name] = {"name": name, "version": fields[1], "filename": filename,
                             "url": direct.split("#", 1)[0], "sha256": digest,
                             "python_abi": fields[2], "platform": fields[4]}
        else:
            version = match.group("version")
            if not version or any(c in version for c in "*<>=~!"):
                raise DependencyLockError(f"ocr-cpu: non-exact pin: {name}")
            versions[name] = version
    if set(vendors) != {"torch", "torchvision"} or "easyocr" not in versions:
        raise DependencyLockError("ocr-cpu: missing CPU vendor wheels or engine")
    if any(name.startswith(("nvidia-", "triton", "rocm", "onnxruntime")) for name in versions):
        raise DependencyLockError("ocr-cpu: accelerator packages are forbidden")
    return versions, vendors


def validate_cpu_ocr_lock(declaration: Path, lock: Path) -> dict[str, str]:
    """Require the generated closure to agree with every source-owned pin."""
    versions, vendors = cpu_ocr_declaration(declaration)
    records = parse_hash_locked_requirements(lock.read_text(encoding="utf-8"), role="ocr-cpu")
    validate_lock_role(records, role="ocr-cpu")
    if {item.name for item in records} != set(versions):
        raise DependencyLockError("ocr-cpu: lock closure differs from declared requirements")
    for item in records:
        expected = versions[item.name]
        if item.name in vendors:
            record = vendors[item.name]
            expected = f"@ {record['url']}#sha256={record['sha256']}"
            if item.hashes != (record["sha256"],):
                raise DependencyLockError(f"ocr-cpu: vendor hash mismatch: {item.name}")
        if item.version != expected:
            raise DependencyLockError(f"ocr-cpu: source/lock version mismatch: {item.name}")
    return versions


NVIDIA_CUDA_MAJOR = 13
_NVIDIA_OCR_PACKAGES = ("easyocr", "torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil")


def _validate_nvidia_runtime_lock(value: Mapping[str, object], repo_root: Path | str | None) -> dict[str, object]:
    """Validate `containers/cognita-nvidia/runtime-lock.json` (DESIGN-NVIDIA-ACCELERATION 5.6).

    Structure first (CUDA 13, the CUDA execution provider, PyPI-only: no provider wheel, no deb, no vendor
    wheel table, triton excluded), then, when a checkout root is given, the hashes of the committed files
    the image copies or is built from."""
    try:
        embedding = value["embedding"]
        for key in ("fastembed", "onnxruntime-gpu"):
            if not isinstance(embedding[key], str) or not embedding[key]:
                raise DependencyLockError(f"NVIDIA embedding pin is malformed: {key}")
        if embedding["python_abi"] != "cp313":
            raise DependencyLockError("NVIDIA embedding runtime must be CPython 3.13 (cp313)")
        if embedding["cuda_major"] != NVIDIA_CUDA_MAJOR:
            raise DependencyLockError("NVIDIA embedding runtime is not CUDA 13")
        if embedding["provider"] != "CUDAExecutionProvider":
            raise DependencyLockError("NVIDIA embedding provider is not CUDA")
        if "provider_wheel" in embedding or "migraphx_deb" in embedding:
            raise DependencyLockError("NVIDIA embedding runtime is PyPI-only: no provider wheel or deb")
        wheels = embedding["nvidia_wheels"]
        if not isinstance(wheels, Mapping) or not wheels:
            raise DependencyLockError("NVIDIA embedding runtime lists no NVIDIA wheels")
        for name, version in wheels.items():
            if not str(name).startswith("nvidia-") or not isinstance(version, str) or not version:
                raise DependencyLockError(f"NVIDIA wheel declaration is malformed: {name}")
        ocr = value["ocr"]
        for package in _NVIDIA_OCR_PACKAGES:
            if not isinstance(ocr[package], str) or not ocr[package]:
                raise DependencyLockError(f"NVIDIA OCR pin is malformed: {package}")
        if ocr["cuda_major"] != NVIDIA_CUDA_MAJOR:
            raise DependencyLockError("NVIDIA OCR runtime is not CUDA 13")
        if ocr["triton"] != "excluded" or "vendor_wheels" in ocr or "triton-rocm" in ocr:
            raise DependencyLockError("NVIDIA OCR runtime must exclude triton and carry no vendor wheels")
        floor = value["driver_floor"]
        if not isinstance(floor, Mapping) or set(floor) != {"linux", "windows_wsl"} or not all(
                isinstance(v, str) and v.isdigit() for v in floor.values()):
            raise DependencyLockError("NVIDIA driver floor is malformed")
        if not isinstance(value["model_source_manifest"], str) or not isinstance(value["runtime_paths"], Mapping):
            raise DependencyLockError("NVIDIA runtime lock is missing its manifest source or runtime paths")
    except KeyError as exc:
        raise DependencyLockError(f"NVIDIA runtime lock is missing field: {exc.args[0]}") from exc
    if repo_root is not None:
        root = Path(repo_root).resolve()
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise DependencyLockError("NVIDIA runtime lock has no artifact bindings")
        for item in ("preflight_source", "dockerfile_source"):
            record = artifacts.get(item)
            if not isinstance(record, Mapping):
                raise DependencyLockError(f"NVIDIA runtime lock missing artifact binding: {item}")
            rel = str(record.get("path", ""))
            target = (root / rel).resolve()
            if target.parent != (root / Path(rel).parent).resolve() or target.is_symlink() or not target.is_file():
                raise DependencyLockError(f"NVIDIA runtime artifact is missing or linked: {rel}")
            expected = str(record.get("sha256", ""))
            # Hashed over LF-canonical bytes so a Windows checkout with CRLF matches the Linux build context.
            actual = hashlib.sha256(target.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            if not HEX64.fullmatch(expected) or actual != expected:
                raise DependencyLockError(f"NVIDIA runtime artifact hash mismatch: {rel}")
    return dict(value)


def validate_nvidia_ocr_lock(declaration: Path, lock: Path) -> dict[str, str]:
    """Require the NVIDIA OCR closure to agree with every source-owned pin (the twin of the CPU check).

    Returns {normalized name: locked base version} for the whole closure. PyPI-only: a direct artifact
    URL is refused. The torch/torchvision entries are the plain PyPI versions; the local `+cu130` that
    `torch.__version__` reports is not in the lock and is accepted only by the manifest generator."""
    value = validate_runtime_lock(declaration)
    embedding, ocr = value.get("embedding"), value.get("ocr")
    if not isinstance(embedding, Mapping) or "cuda_major" not in embedding or not isinstance(ocr, Mapping):
        raise DependencyLockError("ocr-nvidia: the runtime declaration is not the NVIDIA runtime lock")
    records = parse_hash_locked_requirements(lock.read_text(encoding="utf-8"), role="ocr-nvidia")
    validate_lock_role(records, role="ocr-nvidia")
    versions = {item.name: item.version for item in records}
    for item in records:
        if item.version.startswith("@ "):
            raise DependencyLockError(f"ocr-nvidia: direct artifact URLs are not accepted (PyPI only): {item.name}")
    for package in _NVIDIA_OCR_PACKAGES:
        if versions.get(package.lower()) != ocr[package]:
            raise DependencyLockError(f"ocr-nvidia: source/lock version mismatch: {package}")
    return versions


def validate_runtime_lock(path: Path | str, *, repo_root: Path | str | None = None) -> dict[str, object]:
    """Validate the existing AMD runtime lock and hashes of committed artifacts.

    15.0.0: a lock whose embedding section declares `cuda_major` is the NVIDIA lock and takes the NVIDIA
    branch (`_validate_nvidia_runtime_lock`); the AMD messages below still say AMD."""
    lock_path = Path(path).resolve()
    if lock_path.is_symlink() or not lock_path.is_file():
        raise DependencyLockError("AMD runtime lock is missing or linked")
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyLockError("AMD runtime lock is unreadable") from exc
    # 15.0.0: the NVIDIA runtime lock has its own shape (no provider wheel, no deb) and is recognised by
    # its `cuda_major`. The AMD checks below are untouched.
    section = value.get("embedding") if isinstance(value, Mapping) else None
    if isinstance(section, Mapping) and "cuda_major" in section:
        return _validate_nvidia_runtime_lock(value, repo_root)
    try:
        embedding = value["embedding"]
        if embedding["fastembed"] != "0.8.0" or embedding["onnxruntime-migraphx"] != "1.23.1":
            raise DependencyLockError("AMD embedding versions are not the qualified FastEmbed/MIGraphX pair")
        if embedding["provider"] != "MIGraphXExecutionProvider":
            raise DependencyLockError("AMD embedding provider is not MIGraphX")
        wheel = embedding["provider_wheel"]
        if not HEX64.fullmatch(wheel["sha256"]):
            raise DependencyLockError("AMD provider wheel hash is malformed")
        if not HEX64.fullmatch(embedding["migraphx_deb"]["sha256"]):
            raise DependencyLockError("AMD MIGraphX package hash is malformed")
    except KeyError as exc:
        raise DependencyLockError(f"AMD runtime lock is missing field: {exc.args[0]}") from exc
    if repo_root is not None:
        root = Path(repo_root).resolve()
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise DependencyLockError("AMD runtime lock has no artifact bindings")
        for item in ("qualification_manifest", "preflight_source", "dockerfile_source"):
            record = artifacts.get(item)
            if not isinstance(record, Mapping):
                raise DependencyLockError(f"AMD runtime lock missing artifact binding: {item}")
            rel = str(record.get("path", ""))
            target = (root / rel).resolve()
            if target.parent != (root / Path(rel).parent).resolve() or target.is_symlink() or not target.is_file():
                raise DependencyLockError(f"AMD runtime artifact is missing or linked: {rel}")
            expected = str(record.get("sha256", ""))
            if not HEX64.fullmatch(expected) or _sha256(target) != expected:
                raise DependencyLockError(f"AMD runtime artifact hash mismatch: {rel}")
        models = artifacts.get("ocr_models")
        if not isinstance(models, Mapping):
            raise DependencyLockError("AMD runtime lock has no OCR model bindings")
        # 14.2.0 (DESIGN-LINUX-INSTALLER 6.5): the weights are no longer committed, so each binding
        # is a name and a hash and is checked against the qualification manifest (the one authority)
        # instead of a file in the tree. (Superseded: bindings used to carry a repo path whose bytes
        # were hashed here.)
        authority = json.loads((root / str(artifacts["qualification_manifest"]["path"])).read_text(encoding="utf-8"))["model_files"]
        for name, record in models.items():
            if not isinstance(record, Mapping):
                raise DependencyLockError("AMD OCR model binding is malformed")
            expected = str(record.get("sha256", ""))
            if not HEX64.fullmatch(expected) or authority.get(name) != expected:
                raise DependencyLockError(f"AMD OCR model binding mismatch: {name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_locked_install(command: Sequence[str], *, role: str) -> None:
    """Reject an install command that can resolve a fresh dependency graph."""
    text = " ".join(command)
    if "pip install" not in text and "uv pip install" not in text:
        return
    if "--require-hashes" not in command and "--no-index" not in command:
        raise DependencyLockError(f"{role}: dependency installation must use a verified lock or local wheel")
    if any(token in {"-U", "--upgrade", "--upgrade-strategy"} or token.startswith((">", "<", "~=", "~=")) for token in command):
        raise DependencyLockError(f"{role}: dependency upgrade/range resolution is forbidden")


__all__ = [
    "DependencyLockError", "DependencyLockSet", "FASTEMBED_MIGRAPHX_EXCEPTION", "LOCK_FILES",
    "LOCK_ROLES", "LockedRequirement", "assert_locked_install", "load_dependency_locks",
    "parse_hash_locked_requirements", "validate_lock_role", "validate_nvidia_ocr_lock", "validate_runtime_lock",
]
