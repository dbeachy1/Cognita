#!/usr/bin/env python3
"""Deliberately refresh all six hash-checked dependency locks in Docker.

This is an operator-reviewed maintenance command, not part of candidate
qualification. The resolver runs in a task-owned image built from the
repository's pinned Python 3.13 base; the KEI host interpreter and pip-tools
installation are not part of the qualification contract.
"""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import re
import signal
import time
import uuid
import zipfile
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .dependency_locks import LOCK_FILES, LOCK_ROLES, DependencyLockError, load_dependency_locks, parse_hash_locked_requirements, validate_lock_role, cpu_ocr_declaration, validate_cpu_ocr_lock, validate_nvidia_ocr_lock, validate_runtime_lock
except ImportError:  # direct script invocation
    from dependency_locks import LOCK_FILES, LOCK_ROLES, DependencyLockError, load_dependency_locks, parse_hash_locked_requirements, validate_lock_role, cpu_ocr_declaration, validate_cpu_ocr_lock, validate_nvidia_ocr_lock, validate_runtime_lock  # type: ignore[no-redef]


HEX40 = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SourceIdentity:
    """Which checkout produced a lock set, for the refresh evidence file.

    13.0 section 8: this used to come from `scripts/release_identity.py`,
    which is deleted with the rest of the old release machinery.  The refresh
    evidence records the same four fields it always did, so the checked-in
    `containers/dependency-refresh-evidence.json` stays readable; the rest of
    that module -- build-input manifests, artifact keys, qualification
    identity -- had no user here and is gone.
    """

    commit: str
    tree: str
    branch: str
    remote: str

    def __post_init__(self) -> None:
        if not HEX40.fullmatch(self.commit) or not HEX40.fullmatch(self.tree):
            raise RefreshError("source commit and tree must be full 40-character SHA-1 values")
        if not self.remote:
            raise RefreshError("configured Git remote is required")

    def as_dict(self) -> dict[str, str]:
        return {"commit": self.commit, "tree": self.tree, "branch": self.branch, "remote": self.remote}


def _git(root: Path, *args: str, optional: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        if optional:
            return ""
        raise RefreshError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def source_identity(repo: Path | str) -> SourceIdentity:
    root = Path(repo).resolve()
    remote = _git(root, "config", "--get", "remote.origin.url", optional=True)
    if not remote:
        remote = _git(root, "config", "--get-regexp", r"^remote\..*\.url$", optional=True)
        if "\t" in remote:
            remote = remote.split("\t", 1)[1]
    return SourceIdentity(
        commit=_git(root, "rev-parse", "HEAD"),
        tree=_git(root, "rev-parse", "HEAD^{tree}"),
        branch=_git(root, "symbolic-ref", "--short", "-q", "HEAD", optional=True) or "(detached)",
        remote=remote,
    )


class RefreshError(RuntimeError):
    """A deliberate lock refresh could not prove a complete result."""


GENERAL_INDEX_URL = "https://pypi.org/simple"

# 15.0.0: which committed evidence file a role's refresh feeds, and the ONLY roles that file may describe.
# Each file binds the exact source files its own roles consumed, and release.py / the NVIDIA asset test check
# those hashes on every build, so a file that also pinned another image's inputs would break that image's
# builds the next time the other image's files change. A run whose roles feed two files is refused, and a run
# writes only the file(s) its roles feed, each holding only its own roles. (A default run has no `ocr-cpu`,
# so it feeds the NVIDIA file alone, filtered to the two NVIDIA roles, and keeps working.)
EVIDENCE_FILES: Mapping[str, tuple[str, ...]] = {
    "ocr-cpu-refresh-evidence.json": ("ocr-cpu",),
    "nvidia-refresh-evidence.json": ("embedding-nvidia", "ocr-nvidia"),
}


def evidence_files_for(roles: Sequence[str]) -> tuple[str, ...]:
    """The committed evidence files a run with these roles writes; refuse a run that would write two."""
    files = tuple(name for name, owned in EVIDENCE_FILES.items() if set(owned) & set(roles))
    if len(files) > 1:
        raise RefreshError(
            "these roles would write two evidence files (" + " and ".join(files) + "), and each file may record only its own "
            "roles; run them separately: --roles ocr-cpu, then --roles embedding-nvidia,ocr-nvidia"
        )
    return files


@dataclass(frozen=True)
class RefreshRequest:
    source_root: Path
    evidence_root: Path
    index_url: str
    docker: Path = Path("docker")
    timeout: float = 1800
    roles: tuple[str, ...] = LOCK_ROLES
    preserve_existing_pins: bool = False


@dataclass(frozen=True)
class RefreshResult:
    evidence: Path
    locks: Mapping[str, Path]
    source_commit: str


@dataclass(frozen=True)
class ResolverSpec:
    """Source-scoped interpreter/index contract for one lock role."""

    image: str
    python: str
    indexes: tuple[str, ...]
    expected_python: str = "3.13"


# These are source-owned declarations, not operator-selected requirement files.
# Dockerfile/runtime-lock/package inputs are included in the evidence binding,
# while the generated .in text contains only the exact Python declarations that
# the pinned resolver can compile.
ROLE_SOURCE_DECLARATIONS: Mapping[str, tuple[str, ...]] = {
    "service": ("pyproject.toml",),
    "broker": ("containers/workspace-runtime/requirements.in", "containers/workspace-runtime/microsandbox.lock"),
    "embedding": ("containers/cognita-amd/Dockerfile", "containers/cognita-amd/runtime-lock.json"),
    "ocr-cpu": ("containers/cognita/Dockerfile", "containers/cognita/runtime-lock.json", "docs/easyocr-qualification-dependencies.json"),
    "ocr": ("containers/cognita-amd/Dockerfile", "containers/cognita-amd/runtime-lock.json", "docs/easyocr-qualification-dependencies.json"),
    # 15.0.0 (DESIGN-NVIDIA-ACCELERATION 5.6): the two NVIDIA runtimes. The runtime-lock is the source
    # authority for their pins (fastembed, onnxruntime-gpu with its CUDA extras; easyocr, torch,
    # torchvision); the Dockerfile is bound into the evidence like the AMD one.
    "embedding-nvidia": ("containers/cognita-nvidia/Dockerfile", "containers/cognita-nvidia/runtime-lock.json"),
    "ocr-nvidia": ("containers/cognita-nvidia/Dockerfile", "containers/cognita-nvidia/runtime-lock.json", "docs/easyocr-qualification-dependencies.json"),
    "build": ("pyproject.toml", "containers/cognita/Dockerfile", "containers/cognita-amd/Dockerfile", "containers/workspace-runtime/Dockerfile", "containers/workspace-toolbox/Dockerfile", "containers/image-manifest.json"),
    "test": ("pyproject.toml", "containers/workspace-toolbox/packages.lock"),
}

_PYTHON_SPEC = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*(?:==|>=|<=|~=|!=)[^\s]+$")
_MIGRAPHX_URL = "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/onnxruntime_migraphx-1.23.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
_MIGRAPHX_SHA256 = "ccbbcf44b06e57d6b1f103843649a6f6c2d01ed7655133cf25cb441ccde60e35"
_PYTORCH_ROCM_INDEX = "https://download.pytorch.org/whl/test/rocm7.1"
_TORCH_ROCM_URL = "https://download.pytorch.org/whl/test/rocm7.1/torch-2.10.0%2Brocm7.1-cp313-cp313-manylinux_2_28_x86_64.whl"
_TORCH_ROCM_SHA256 = "3f18b06f233d4871baaafdb8ba662ae69ff91ec11aa1bf76ca76dde31b43e9cd"
_TORCHVISION_ROCM_URL = "https://download.pytorch.org/whl/test/rocm7.1/torchvision-0.25.0%2Brocm7.1-cp313-cp313-manylinux_2_28_x86_64.whl"
_TORCHVISION_ROCM_SHA256 = "54c06f29e20efa36522eedcc5a0fb6f03627ff0ff23cc7bef46f71a3042db074"
_TRITON_ROCM_URL = "https://download.pytorch.org/whl/test/triton_rocm-3.6.0-cp313-cp313-linux_x86_64.whl"
_TRITON_ROCM_SHA256 = "21c0d650cdcf91cb5ae1d18c6dfc027b8238d49cd08212b9fcf77f27cc04e752"
_TRITON_ROCM_METADATA_SHA256 = "5c924120cb0a24aa3979be779d0fa0d2945460dde676e56abb53fcc9244f52f7"
_OCR_VENDOR_WHEEL_CONTRACT: Mapping[str, Mapping[str, str]] = {
    "torch": {
        "name": "torch",
        "version": "2.10.0+rocm7.1",
        "filename": "torch-2.10.0+rocm7.1-cp313-cp313-manylinux_2_28_x86_64.whl",
        "url": _TORCH_ROCM_URL,
        "python_abi": "cp313",
        "platform": "manylinux_2_28_x86_64",
        "sha256": _TORCH_ROCM_SHA256,
    },
    "torchvision": {
        "name": "torchvision",
        "version": "0.25.0+rocm7.1",
        "filename": "torchvision-0.25.0+rocm7.1-cp313-cp313-manylinux_2_28_x86_64.whl",
        "url": _TORCHVISION_ROCM_URL,
        "python_abi": "cp313",
        "platform": "manylinux_2_28_x86_64",
        "sha256": _TORCHVISION_ROCM_SHA256,
    },
    "triton-rocm": {
        "name": "triton-rocm",
        "version": "3.6.0",
        "filename": "triton_rocm-3.6.0-cp313-cp313-linux_x86_64.whl",
        "url": _TRITON_ROCM_URL,
        "python_abi": "cp313",
        "platform": "linux_x86_64",
        "sha256": _TRITON_ROCM_SHA256,
        "metadata_sha256": _TRITON_ROCM_METADATA_SHA256,
    },
}
_RESOLVER_TOOLING_LOCK = (
    "pip-tools==7.6.1 --hash=sha256:6111c8b4b07fd14b7223ca921485b0e96cf66e20bf94da95eeed9845f510cb8f",
    "pip==26.2 --hash=sha256:931c303696af6fa3417112103b1cad26890e5a07eccb5b99783700e33f2b8aad",
    "build==1.3.0 --hash=sha256:7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4",
    "click==8.1.8 --hash=sha256:63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2",
    "pyproject-hooks==1.2.0 --hash=sha256:9e5c6bfa8dcc30091c74b0cf803c81fdd29d94f01992a7707bc97babb1141913",
    "setuptools==80.9.0 --hash=sha256:062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922",
    "wheel==0.45.1 --hash=sha256:708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248",
    "packaging==25.0 --hash=sha256:29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def role_source_paths(root: Path, roles: Sequence[str] = LOCK_ROLES) -> dict[str, tuple[Path, ...]]:
    """Resolve the six source-owned role declarations from the checkout."""
    base = root.resolve()
    result: dict[str, tuple[Path, ...]] = {}
    for role in roles:
        declarations = ROLE_SOURCE_DECLARATIONS.get(role)
        if not declarations:
            raise RefreshError(f"source-owned dependency role has no declaration: {role}")
        paths: list[Path] = []
        for relative in declarations:
            path = (base / relative).resolve()
            if not _inside(base, path) or path.is_symlink() or not path.is_file():
                raise RefreshError(f"{role} source input is missing or linked: {relative}")
            paths.append(path)
        result[role] = tuple(paths)
    return result


def parse_role_sources(values: Sequence[str]) -> dict[str, Path]:
    """Reject the retired caller-selected source interface."""
    if values:
        raise RefreshError("role sources are source-owned; do not supply --role-source")
    return {}


def _dockerfile_specs(path: Path) -> list[str]:
    specs: list[str] = []
    active = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if "pip install" in line:
            active = True
        if active:
            for token in re.findall(r"[\"']?([A-Za-z][A-Za-z0-9_.-]*(?:==|>=|<=|~=|!=)[^\"'\s]+)[\"']?", line):
                if _PYTHON_SPEC.fullmatch(token) and token not in specs:
                    specs.append(token)
        if active and line and not line.endswith("\\") and "pip install" not in line:
            active = False
    return specs


def _toml_requirements(path: Path, *, extra: str | None = None, build: bool = False) -> list[str]:
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if build:
        values = data.get("build-system", {}).get("requires", [])
    elif extra:
        values = data.get("project", {}).get("optional-dependencies", {}).get(extra, [])
    else:
        values = data.get("project", {}).get("dependencies", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise RefreshError(f"pyproject dependency declarations are malformed: {path}")
    return list(dict.fromkeys(values))


def _runtime_lock_requirements(path: Path, key: str) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshError(f"runtime dependency lock is unreadable: {path}") from exc
    group = value.get(key) if isinstance(value, Mapping) else None
    if not isinstance(group, Mapping):
        raise RefreshError(f"runtime dependency lock has no {key} declarations: {path}")
    runtime_metadata = {"python_abi", "provider", "model_manifest"}
    result = []
    for name, version in group.items():
        if (
            isinstance(name, str)
            and name not in runtime_metadata
            and isinstance(version, str)
            and re.fullmatch(r"[0-9][A-Za-z0-9.+!-]*", version)
            and _PYTHON_SPEC.fullmatch(f"{name}=={version}")
        ):
            result.append(f"{name}=={version}")
    if not result:
        raise RefreshError(f"runtime dependency lock has no Python declarations: {key}")
    return result


def _ocr_vendor_wheels(path: Path) -> dict[str, dict[str, str]]:
    """Validate the source-owned OCR vendor records against the approved contract."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        ocr = value["ocr"]
        vendor = ocr["vendor_wheels"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RefreshError("OCR vendor wheel declarations are unreadable") from exc
    if ocr.get("triton-rocm") != _OCR_VENDOR_WHEEL_CONTRACT["triton-rocm"]["version"]:
        raise RefreshError("OCR runtime lock does not pin the approved triton-rocm version")
    if not isinstance(vendor, Mapping) or set(vendor) != {"triton-rocm"}:
        raise RefreshError("OCR vendor wheel declarations are not the approved exact set")
    result: dict[str, dict[str, str]] = {}
    for name, expected in _OCR_VENDOR_WHEEL_CONTRACT.items():
        if name == "triton-rocm":
            raw = vendor.get(name)
            if not isinstance(raw, Mapping):
                raise RefreshError("OCR triton-rocm vendor wheel declaration is malformed")
            actual = {field: str(raw.get(field, "")) for field in expected if field != "name"}
            actual["name"] = str(raw.get("name", name))
        else:
            actual = dict(expected)
        if actual != dict(expected):
            raise RefreshError(f"OCR vendor wheel declaration does not match the approved {name} artifact")
        result[name] = actual
    return result


def _role_input_text(root: Path, role: str, paths: Sequence[Path]) -> str:
    by_name = {path.relative_to(root).as_posix(): path for path in paths}
    if role == "service":
        specs = _toml_requirements(by_name["pyproject.toml"])
    elif role == "test":
        specs = _toml_requirements(by_name["pyproject.toml"], extra="dev")
    elif role == "build":
        specs = _toml_requirements(by_name["pyproject.toml"], build=True)
    elif role == "broker":
        specs = [line.strip() for line in by_name["containers/workspace-runtime/requirements.in"].read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    elif role == "embedding":
        specs = _runtime_lock_requirements(by_name["containers/cognita-amd/runtime-lock.json"], "embedding")
        url, digest = _embedding_artifact(root)
        specs = [item for item in specs if not item.startswith("onnxruntime-migraphx==")]
        specs.append(f"onnxruntime-migraphx @ {url}#sha256={digest}")
    elif role == "embedding-nvidia":
        specs = _runtime_lock_requirements(by_name["containers/cognita-nvidia/runtime-lock.json"], "embedding")
        # The CUDA provider's runtime libraries are the wheel's own [cuda,cudnn] extras (nvrtc, cudart,
        # cufft, curand, cudnn, and cublas/nvjitlink through them), resolved and hash-pinned from PyPI.
        specs = [f"onnxruntime-gpu[cuda,cudnn]=={item.split('==', 1)[1]}" if item.startswith("onnxruntime-gpu==") else item
                 for item in specs]
        if not any(item.startswith("onnxruntime-gpu[") for item in specs):
            raise RefreshError("NVIDIA runtime lock does not pin onnxruntime-gpu")
    elif role == "ocr-nvidia":
        # triton is "excluded" in the runtime lock (not a version), so it never reaches the resolver's
        # input; torch's own requirement for it is resolved and then dropped by _omit_torch_triton.
        specs = _runtime_lock_requirements(by_name["containers/cognita-nvidia/runtime-lock.json"], "ocr")
    elif role == "ocr-cpu":
        declaration = by_name["containers/cognita/runtime-lock.json"]
        cpu_ocr_declaration(declaration)
        specs = json.loads(declaration.read_text(encoding="utf-8"))["requirements"]
    elif role == "ocr":
        specs = _runtime_lock_requirements(by_name["containers/cognita-amd/runtime-lock.json"], "ocr")
        vendor_records = _ocr_vendor_wheels(by_name["containers/cognita-amd/runtime-lock.json"])
        vendor = {
            f"{name}=={record['version']}": f"{name} @ {record['url']}#sha256={record['sha256']}"
            for name, record in vendor_records.items()
        }
        specs = [vendor.get(item, item) for item in specs]
    else:
        raise RefreshError(f"unknown dependency role: {role}")
    if not specs:
        raise RefreshError(f"source-owned role has no Python declarations: {role}")
    return "\n".join(dict.fromkeys(specs)) + "\n"


def _validate_ocr_wheel(path: Path, record: Mapping[str, str]) -> dict[str, object]:
    """Verify downloaded bytes and the wheel's embedded distribution metadata."""
    if path.name != record["filename"] or not path.is_file() or path.is_symlink():
        raise RefreshError(f"OCR vendor wheel filename is not approved: {path.name}")
    digest = _sha256(path)
    if digest != record["sha256"]:
        raise RefreshError(f"OCR vendor wheel hash mismatch: {record['filename']}")
    expected_tag = f"{record['python_abi']}-{record['python_abi']}-{record['platform']}"
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in wheel.namelist() if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise RefreshError(f"OCR vendor wheel metadata is ambiguous: {record['filename']}")
            metadata_bytes = wheel.read(metadata_names[0])
            wheel_text = wheel.read(wheel_names[0]).decode("utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RefreshError(f"OCR vendor wheel is not a readable wheel: {record['filename']}") from exc
    metadata = Parser().parsestr(metadata_bytes.decode("utf-8"))
    if metadata.get("Name", "").lower().replace("_", "-") != record["name"]:
        raise RefreshError(f"OCR vendor wheel metadata name mismatch: {record['filename']}")
    if metadata.get("Version") != record["version"]:
        raise RefreshError(f"OCR vendor wheel metadata version mismatch: {record['filename']}")
    tags = {line.split(":", 1)[1].strip() for line in wheel_text.splitlines() if line.startswith("Tag:") and ":" in line}
    if expected_tag not in tags:
        raise RefreshError(f"OCR vendor wheel ABI/platform tag mismatch: {record['filename']}")
    if record["version"].endswith("+cpu"):
        python_constraint = metadata.get("Requires-Python", "")
        # The explicit vendor wheel target is cp313, not cp313t. pip-compile
        # verifies the complete dependency constraints in that same interpreter.
        for constraint in python_constraint.split(","):
            match = re.fullmatch(r"\s*(>=|<=|>|<|==|!=)\s*(\d+)\.(\d+)(?:\.\d+)?\s*", constraint)
            if not match:
                raise RefreshError(f"OCR vendor Python requirement is unsupported: {record['filename']}")
            target, bound = (3, 13), (int(match[2]), int(match[3]))
            allowed = {">=": target >= bound, "<=": target <= bound, ">": target > bound,
                       "<": target < bound, "==": target == bound, "!=": target != bound}
            if not allowed[match[1]]:
                raise RefreshError(f"OCR vendor Python requirement rejects cp313: {record['filename']}")
    metadata_digest = hashlib.sha256(metadata_bytes).hexdigest()
    expected_metadata_digest = record.get("metadata_sha256")
    if expected_metadata_digest and metadata_digest != expected_metadata_digest:
        raise RefreshError(f"OCR vendor wheel METADATA hash mismatch: {record['filename']}")
    return {
        "name": record["name"],
        "version": record["version"],
        "filename": record["filename"],
        "url": record["url"],
        "python_abi": record["python_abi"],
        "platform": record["platform"],
        "sha256": digest,
        "metadata_sha256": metadata_digest,
        "wheel_tags": sorted(tags),
    }


def _docker_arg(path: Path, name: str) -> str:
    match = re.search(rf"(?m)^ARG\s+{re.escape(name)}=([^\s]+)\s*$", path.read_text(encoding="utf-8"))
    if not match or "@sha256:" not in match.group(1):
        raise RefreshError(f"pinned {name} resolver image is missing from {path}")
    return match.group(1)


def _resolver_specs(root: Path, roles: Sequence[str] = LOCK_ROLES) -> dict[str, ResolverSpec]:
    """Inspect only the selected roles' actual interpreter owners."""
    result = {}
    for role in roles:
        if role == "broker":
            image = _docker_arg(root / "containers/workspace-runtime/Dockerfile", "MICROSANDBOX_IMAGE")
            result[role] = ResolverSpec(image, "/usr/bin/python3", ("pypi",), "3.12")
        elif role == "embedding":
            image = _docker_arg(root / "containers/cognita-amd/Dockerfile", "PYTHON_BASE_IMAGE")
            result[role] = ResolverSpec(image, "/usr/bin/python3", ("pypi",), "3.12")
        elif role in {"embedding-nvidia", "ocr-nvidia"}:
            # 15.0.0: both NVIDIA runtimes are Python 3.13 venvs on the CPU image's base (one interpreter),
            # so they resolve on that exact digest, from PyPI alone.
            image = _docker_arg(root / "containers/cognita/Dockerfile", "PYTHON_BASE_IMAGE")
            result[role] = ResolverSpec(image, "/usr/local/bin/python", ("pypi",), "3.13")
        else:
            image = _docker_arg(root / "containers/cognita/Dockerfile", "PYTHON_BASE_IMAGE")
            result[role] = ResolverSpec(image, "/usr/local/bin/python", ("pypi",), "3.13")
    return result


def _embedding_artifact(root: Path) -> tuple[str, str]:
    runtime = root / "containers/cognita-amd/runtime-lock.json"
    try:
        value = json.loads(runtime.read_text(encoding="utf-8"))
        wheel = value["embedding"]["provider_wheel"]
        origin, filename, digest = str(wheel["origin"]).rstrip("/"), str(wheel["filename"]), str(wheel["sha256"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RefreshError("AMD embedding artifact declaration is unreadable") from exc
    url = f"{origin}/{filename}"
    parsed = urlsplit(url)
    if (
        url != _MIGRAPHX_URL
        or digest != _MIGRAPHX_SHA256
        or filename != "onnxruntime_migraphx-1.23.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
        or parsed.scheme != "https"
        or parsed.query
        or "cp312" not in filename
    ):
        raise RefreshError("AMD embedding artifact does not match the approved CPython 3.12 wheel")
    dockerfile = (root / "containers/cognita-amd/Dockerfile").read_text(encoding="utf-8")
    if f"EMBEDDING_PROVIDER_WHEEL_URL={url}" not in dockerfile or f"EMBEDDING_PROVIDER_WHEEL_SHA256={digest}" not in dockerfile:
        raise RefreshError("AMD Dockerfile disagrees with the source-owned embedding artifact")
    return url, digest


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_request(request: RefreshRequest) -> None:
    if not request.roles or len(set(request.roles)) != len(request.roles) or set(request.roles) - set(LOCK_FILES):
        raise RefreshError("roles must be a nonempty, unique selection of known roles")
    evidence_files_for(request.roles)
    if not 0 < request.timeout <= 1800:
        raise RefreshError("refresh timeout must be within the 1800-second maintenance budget")
    if type(request.preserve_existing_pins) is not bool:
        raise RefreshError("preserve_existing_pins must be a boolean")
    if platform.system() != "Linux":
        raise RefreshError("dependency refresh is Linux/KEI-only")
    root = request.source_root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise RefreshError(f"source checkout is missing or linked: {root}")
    evidence = request.evidence_root.resolve()
    if request.evidence_root.exists() and request.evidence_root.is_symlink():
        raise RefreshError("refresh evidence root cannot be linked")
    if evidence == root or root in evidence.parents:
        raise RefreshError("refresh evidence must be outside the source checkout")
    if not evidence.parent.is_dir():
        raise RefreshError(f"refresh evidence parent is missing: {evidence.parent}")
    if request.index_url != GENERAL_INDEX_URL:
        raise RefreshError(f"dependency refresh requires the code-owned general index {GENERAL_INDEX_URL}")
    parsed_url = urlsplit(request.index_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise RefreshError("dependency index URL must be an http(s) URL without embedded credentials")
    if str(request.docker) == "docker" and shutil.which("docker") is None:
        raise RefreshError("Docker Engine is unavailable")
    if str(request.docker) != "docker" and not request.docker.is_file():
        raise RefreshError(f"Docker executable is unavailable: {request.docker}")
    role_source_paths(root, request.roles)
    tooling_lock = root / "containers/resolver-tooling.lock"
    if tooling_lock.is_symlink() or not tooling_lock.is_file():
        raise RefreshError("resolver-tooling.lock is missing or linked")
    actual_tooling = tuple(line.strip() for line in tooling_lock.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
    if actual_tooling != _RESOLVER_TOOLING_LOCK:
        raise RefreshError("resolver-tooling.lock does not match the selected eight-wheel toolchain")
    parse_hash_locked_requirements(tooling_lock.read_text(encoding="utf-8"), role="resolver-tooling")
    _resolver_specs(root, request.roles)
    if "embedding" in request.roles:
        _embedding_artifact(root)
    if "ocr" in request.roles:
        _ocr_vendor_wheels(root / "containers/cognita-amd/runtime-lock.json")
    if "ocr-cpu" in request.roles:
        cpu_ocr_declaration(root / "containers/cognita/runtime-lock.json")
    if set(request.roles) & {"embedding-nvidia", "ocr-nvidia"}:
        # Structure only (no repo_root): the Dockerfile and preflight hashes it binds are checked by the
        # test suite, so a lock refresh is not blocked while those files are still being edited.
        try:
            validate_runtime_lock(root / "containers/cognita-nvidia/runtime-lock.json")
        except DependencyLockError as exc:
            raise RefreshError(f"NVIDIA runtime lock is not usable as a refresh source: {exc}") from exc


def _clean_environment(temp_root: Path) -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "HOME": str(temp_root / "home"),
            "XDG_CACHE_HOME": str(temp_root / "cache"),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_CACHE_DIR": str(temp_root / "pip-cache"),
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def _seed_existing_lock(root: Path, role: str, output: Path) -> str:
    """Seed pip-compile output with an existing role lock to retain compatible pins."""
    source = root / "containers" / LOCK_FILES[role]
    if source.is_symlink() or not source.is_file():
        raise RefreshError(f"cannot preserve pins from a missing or linked lock: {source}")
    parse_hash_locked_requirements(source.read_text(encoding="utf-8"), role=role)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    return _sha256(source)


def _verify_owned_scratch(root: Path) -> None:
    """Reject resolver output that escaped the invoking user's ownership."""
    if not hasattr(os, "getuid"):
        return
    owner = os.getuid()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RefreshError(f"resolver scratch contains an unexpected link: {path}")
        if path.stat().st_uid != owner:
            raise RefreshError(f"resolver scratch is not user-owned: {path}")


def _canonicalize_compiled_lock(text: str, *, role: str) -> str:
    """Collapse pip-tools hash continuations into the lock parser's one-line form."""
    records: list[str] = []
    pending = ""
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            if not line.startswith("--hash=sha256:"):
                raise RefreshError(f"generated {role} lock has an invalid continuation at line {line_number}")
            pending += " " + line.rstrip("\\").rstrip()
            if line.endswith("\\"):
                continue
            records.append(pending)
            pending = ""
            continue
        if line.startswith("--"):
            raise RefreshError(f"generated {role} lock emitted an unsupported pip option at line {line_number}")
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        records.append(line)
    if pending:
        raise RefreshError(f"generated {role} lock has a dangling continuation")
    if not records:
        raise RefreshError(f"generated {role} lock is empty")
    return "\n".join(records) + "\n"


def _omit_fastembed_cpu_onnxruntime(text: str, *, role: str) -> str:
    """Keep FastEmbed's qualified MIGraphX namespace exception explicit.

    15.0.0: the same omission applies to `embedding-nvidia`, where onnxruntime-gpu owns the
    `onnxruntime` namespace and the pip check exempts FastEmbed's CPU-onnxruntime line."""
    if role not in {"embedding", "embedding-nvidia"}:
        return text
    records = [line for line in text.splitlines() if not re.match(r"^onnxruntime(?:\[[^]]+\])?==", line)]
    if not records:
        raise RefreshError("generated embedding lock lost all requirements while omitting CPU onnxruntime")
    return "\n".join(records) + "\n"


def _omit_torch_triton(text: str, *, role: str) -> str:
    """Drop torch's requirement on triton from the NVIDIA OCR lock (DESIGN-NVIDIA-ACCELERATION 5.4).

    The triton wheel bundles CUDA developer binaries (ptxas, cuobjdump, nvdisasm, CUPTI trees) that the
    CUDA EULA does not list as redistributable, and EasyOCR inference never compiles a kernel. The
    image's `pip check` exempts the one resulting line, "torch ... requires triton"."""
    if role != "ocr-nvidia":
        return text
    records = [line for line in text.splitlines() if not re.match(r"^triton(?:\[[^]]+\])?==", line)]
    if not records:
        raise RefreshError("generated ocr-nvidia lock lost all requirements while omitting triton")
    return "\n".join(records) + "\n"


def _run(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = list(map(str, command))
    try:
        if runner is subprocess.run:
            process = subprocess.Popen(
                argv, cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, stdin=subprocess.PIPE if input_text is not None else None,
                start_new_session=(os.name == "posix"),
            )
            try:
                stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=10)
                raise RefreshError(f"refresh command timed out and was reaped: {' '.join(argv)}") from exc
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        else:
            result = runner(
                argv, cwd=str(cwd), env=dict(env), capture_output=True, text=True,
                input=input_text, timeout=timeout, check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefreshError(f"refresh command could not run: {' '.join(map(str, command))}") from exc
    if result.returncode:
        output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
        detail = " | ".join(line.strip() for line in output.strip().splitlines()[-8:] if line.strip()) or "no output"
        raise RefreshError(f"refresh command failed ({result.returncode}): {detail}")
    return result


def _publish_lock_set(staged: Mapping[str, Path], targets: Mapping[str, Path], backup_root: Path) -> None:
    """Publish the six validated locks as one recoverable set.

    Each individual replacement is atomic, but the set is only accepted when
    every replacement succeeds.  If a later replacement fails, restore the
    exact pre-refresh bytes (or absence) before reporting the failure.
    """
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: dict[str, Path] = {}
    existed: dict[str, bool] = {}
    for role, target in targets.items():
        existed[role] = target.exists()
        if target.exists():
            backup = backup_root / f"{role}.lock.backup"
            shutil.copyfile(target, backup)
            backups[role] = backup
    temporary: dict[str, Path] = {}
    try:
        for role, target in targets.items():
            temp = target.with_name(f".{target.name}.refresh-{os.getpid()}-{uuid.uuid4().hex}")
            temporary[role] = temp
            shutil.copyfile(staged[role], temp)
        for role, target in targets.items():
            os.replace(temporary[role], target)
    except Exception:
        for role, target in targets.items():
            restore = backups.get(role)
            if restore is not None:
                recovery = target.with_name(f".{target.name}.restore-{os.getpid()}-{uuid.uuid4().hex}")
                shutil.copyfile(restore, recovery)
                os.replace(recovery, target)
            elif not existed[role] and (target.exists() or target.is_symlink()):
                target.unlink()
        raise
    finally:
        for path in temporary.values():
            if path.exists() or path.is_symlink():
                path.unlink()


def _write_resolver_dockerfile(path: Path, *, role: str, spec: ResolverSpec, index_url: str) -> None:
    """Build a resolver image with the exact eight-wheel toolchain offline."""
    bootstrap = ""
    if role in {"broker", "embedding"}:
        bootstrap = "RUN apt-get update && apt-get install --no-install-recommends -y python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*\n"
    lines = [
        "ARG RESOLVER_IMAGE\n",
        "FROM ${RESOLVER_IMAGE}\n",
        f"ARG INDEX_URL={index_url}\n",
        f"ARG EXPECTED_PYTHON={spec.expected_python}\n",
        "ENV PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONNOUSERSITE=1 HOME=/opt/cognita-resolver-home\n",
        bootstrap,
        "COPY resolver-tooling.lock /opt/cognita-locks/resolver-tooling.lock\n",
        f"RUN {spec.python} -c \"import sys; assert f'{sys.version_info.major}.{{sys.version_info.minor}}' == '$EXPECTED_PYTHON', sys.version\"\n",
        "RUN mkdir -p /opt/cognita-resolver-wheels /opt/cognita-resolver-home \\\n",
        f"    && {spec.python} -m pip download --isolated --no-deps --only-binary=:all: --require-hashes --index-url \"$INDEX_URL\" --dest /opt/cognita-resolver-wheels -r /opt/cognita-locks/resolver-tooling.lock \\\n",
        f"    && {spec.python} -m venv /opt/cognita-resolver \\\n",
        "    && /opt/cognita-resolver/bin/python -m pip --isolated install --no-index --find-links=/opt/cognita-resolver-wheels --only-binary=:all: --require-hashes --no-deps --no-cache-dir --force-reinstall -r /opt/cognita-locks/resolver-tooling.lock \\\n",
        "    && /opt/cognita-resolver/bin/python -m pip check\n",
        "RUN /opt/cognita-resolver/bin/python -c \"import importlib.metadata as m; expected={'pip-tools':'7.6.1','pip':'26.2','build':'1.3.0','click':'8.1.8','pyproject-hooks':'1.2.0','setuptools':'80.9.0','wheel':'0.45.1','packaging':'25.0'}; assert {k:m.version(k) for k in expected} == expected\"\n",
    ]
    path.write_text("".join(lines), encoding="utf-8")


def _download_and_validate_ocr_wheels(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    docker: Path,
    image_tag: str,
    scratch: Path,
    resolver_environment: Sequence[str],
    resolver_user: str | None,
    resolver_python: str,
    records: Mapping[str, Mapping[str, str]],
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    resource_label: str,
    deadline: float | None = None,
    vendor_subdir: str = "ocr-vendor",
) -> dict[str, object]:
    """Acquire each approved OCR wheel directly, then verify its complete bytes."""
    deadline = deadline or time.monotonic() + timeout
    destination = scratch / vendor_subdir
    destination.mkdir()
    validated: dict[str, object] = {}
    for name, record in records.items():
        requirement = f"{name} @ {record['url']}#sha256={record['sha256']}"
        command = (
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--label",
            resource_label,
            *(('--user', resolver_user) if resolver_user else ()),
            *resolver_environment,
            "--entrypoint",
            resolver_python,
            "--mount",
            f"type=bind,src={scratch},dst=/work",
            "--network=default",
            image_tag,
            "-m",
            "pip",
            "download",
            "--isolated",
            "--disable-pip-version-check",
            "--no-deps",
            "--only-binary=:all:",
            "--no-index",
            "--no-cache-dir",
            "--dest",
            f"/work/{vendor_subdir}",
            requirement,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RefreshError(f"vendor acquisition deadline exhausted: {name}")
        _run(runner, command, cwd=cwd, env=env, timeout=remaining)
        candidates = sorted(destination.glob("*.whl"))
        matching = [path for path in candidates if path.name == record["filename"]]
        if len(matching) != 1 or len(candidates) != len(validated) + 1:
            raise RefreshError(f"OCR vendor wheel download did not produce the approved {name} artifact")
        validated[name] = _validate_ocr_wheel(matching[0], record)
    if {path.name for path in destination.glob("*.whl")} != {record["filename"] for record in records.values()}:
        raise RefreshError("OCR vendor wheel download produced an unexpected artifact")
    return validated


def refresh(
    request: RefreshRequest,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RefreshResult:
    """Resolve all roles in one owned Python 3.13 Docker environment."""
    _validate_request(request)
    deadline = time.monotonic() + request.timeout
    root = request.source_root.resolve()
    evidence = request.evidence_root.resolve()
    scratch = Path(tempfile.mkdtemp(prefix="cognita-dependency-refresh-"))
    atomic_temps: list[Path] = []
    evidence_tmp: Path | None = None
    image_tags: dict[str, str] = {}
    resource_label = f"cognita.dependency-refresh={uuid.uuid4().hex}"
    cleanup_error: str | None = None
    try:
        env = _clean_environment(scratch)
        source = source_identity(root)
        role_paths = role_source_paths(root, request.roles)
        consumed_paths = set(path for paths in role_paths.values() for path in paths)
        consumed_paths.update((root / "containers/resolver-tooling.lock", Path(__file__), Path(__file__).with_name("dependency_locks.py")))
        input_hashes = {str(path.resolve()): _sha256(path) for path in consumed_paths}
        def remaining() -> float:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise RefreshError("dependency maintenance deadline exhausted")
            return budget
        staged: dict[str, Path] = {}
        records: dict[str, Any] = {}
        preserved_pin_seeds: dict[str, str] = {}
        for directory in ("home", "cache", "pip-cache"):
            (scratch / directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "containers/resolver-tooling.lock", scratch / "resolver-tooling.lock")
        resolver_user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else None
        resolver_environment = (
            "--env",
            "HOME=/work/home",
            "--env",
            "XDG_CACHE_HOME=/work/cache",
            "--env",
            "PIP_CACHE_DIR=/work/pip-cache",
        )
        resolver_entrypoint = ("--entrypoint", "/opt/cognita-resolver/bin/pip-compile")
        for role, paths in role_paths.items():
            (scratch / f"{role}.in").write_text(_role_input_text(root, role, paths), encoding="utf-8")
        specs = _resolver_specs(root, request.roles)
        tool_versions: dict[str, str] = {}
        for role in request.roles:
            sources = role_paths[role]
            spec = specs[role]
            image_tag = f"cognita-dependency-refresh-{role}:{uuid.uuid4().hex}"
            image_tags[role] = image_tag
            resolver_dockerfile = scratch / f"Dockerfile.{role}"
            _write_resolver_dockerfile(resolver_dockerfile, role=role, spec=spec, index_url=request.index_url)
            _run(runner, (request.docker, "build", "--pull=false", "--label", resource_label, "--build-arg", f"RESOLVER_IMAGE={spec.image}", "--build-arg", f"EXPECTED_PYTHON={spec.expected_python}", "--tag", image_tag, "--file", resolver_dockerfile, scratch), cwd=root, env=env, timeout=remaining())
            version_result = _run(
                runner,
                (request.docker, "run", "--rm", "--pull=never", "--label", resource_label, *(('--user', resolver_user) if resolver_user else ()), *resolver_environment, *resolver_entrypoint, "--mount", f"type=bind,src={scratch},dst=/work", image_tag, "--version"),
                cwd=root, env=env, timeout=60,
            )
            tool_versions[role] = (version_result.stdout or version_result.stderr).strip()
            artifact_validation: dict[str, object] = {}
            if role in {"ocr", "ocr-cpu"}:
                artifact_validation = _download_and_validate_ocr_wheels(
                    runner,
                    docker=request.docker,
                    image_tag=image_tag,
                    scratch=scratch,
                    resolver_environment=resolver_environment,
                    resolver_user=resolver_user,
                    resolver_python=spec.python,
                    records=(_ocr_vendor_wheels(root / "containers/cognita-amd/runtime-lock.json") if role == "ocr" else cpu_ocr_declaration(root / "containers/cognita/runtime-lock.json")[1]),
                    cwd=root,
                    env=env,
                    timeout=remaining(),
                    resource_label=resource_label,
                    deadline=deadline,
                    vendor_subdir=f"{role}-vendor",
                )
            output = scratch / f"{role}.lock"
            if request.preserve_existing_pins:
                preserved_pin_seeds[role] = _seed_existing_lock(root, role, output)
            command: tuple[object, ...] = (
                request.docker,
                "run",
                "--rm",
                "--pull=never",
                "--label",
                resource_label,
                *(('--user', resolver_user) if resolver_user else ()),
                *resolver_environment,
                *resolver_entrypoint,
                "--mount",
                f"type=bind,src={root},dst=/src,readonly",
                "--mount",
                f"type=bind,src={scratch},dst=/work",
                "--network=default",
                image_tag,
                "--generate-hashes",
                "--resolver=backtracking",
                "--no-emit-index-url",
                "--no-emit-trusted-host",
                "--output-file",
                f"/work/{role}.lock",
                "--index-url",
                request.index_url,
                *(('--allow-unsafe',) if role in {"build", "ocr", "ocr-cpu", "ocr-nvidia"} else ()),
                f"/work/{role}.in",
            )
            _run(runner, command, cwd=root, env=env, timeout=remaining())
            if output.is_symlink() or not output.is_file():
                raise RefreshError(f"pip-compile produced no regular lock for {role}")
            try:
                canonical = _canonicalize_compiled_lock(output.read_text(encoding="utf-8"), role=role)
                canonical = _omit_fastembed_cpu_onnxruntime(canonical, role=role)
                canonical = _omit_torch_triton(canonical, role=role)
                output.write_text(canonical, encoding="utf-8")
                parsed = parse_hash_locked_requirements(canonical, role=role)
                validate_lock_role(parsed, role=role)
                if role == "ocr-cpu":
                    validate_cpu_ocr_lock(root / "containers/cognita/runtime-lock.json", output)
                if role == "ocr-nvidia":
                    validate_nvidia_ocr_lock(root / "containers/cognita-nvidia/runtime-lock.json", output)
                print(f"dependency-refresh: role={role} packages={len(parsed)} lock_sha256={_sha256(output)[:16]}", flush=True)
            except (OSError, DependencyLockError) as exc:
                raise RefreshError(f"generated {role} lock is not complete/hash-checked: {exc}") from exc
            staged[role] = output
            records[role] = {
                "source_inputs": [path.relative_to(root).as_posix() for path in sources],
                "source_sha256": {path.relative_to(root).as_posix(): _sha256(path) for path in sources},
                "input_sha256": _sha256(scratch / f"{role}.in"),
                "lock_sha256": _sha256(output),
                "packages": len(parsed),
                "preserve_existing_pins": request.preserve_existing_pins,
                "preserved_pin_seed_sha256": preserved_pin_seeds.get(role),
                "resolver": {
                    "image": spec.image,
                    "python": spec.python,
                    "expected_python": spec.expected_python,
                    "indexes": [request.index_url],
                    "vendor_artifacts": artifact_validation,
                    "tool_version": tool_versions[role],
                },
            }
        # Re-parse the complete generated set before touching any committed lock.
        for role, output in staged.items():
            parse_hash_locked_requirements(output.read_text(encoding="utf-8"), role=role)
        targets: dict[str, Path] = {}
        containers = root / "containers"
        for role in request.roles:
            name = LOCK_FILES[role]
            target = containers / name
            if target.exists() and target.is_symlink():
                raise RefreshError(f"refusing to replace linked lock: {target}")
            targets[role] = target
        if (
            source_identity(root) != source
            or any(_sha256(Path(name)) != digest for name, digest in input_hashes.items())
            or any(
                _sha256(root / "containers" / LOCK_FILES[role]) != digest
                for role, digest in preserved_pin_seeds.items()
            )
        ):
            raise RefreshError("dependency source inputs changed during maintenance")
        evidence.mkdir(parents=True, exist_ok=True)
        evidence_payload = {
            "schema": 1,
            "operation": "dependency-refresh",
            "source": source.as_dict(),
            "selected_roles": list(request.roles),
            "preserve_existing_pins": request.preserve_existing_pins,
            # Provenance only. The refresh replaces each seeded lock at this
            # path, so its hash is not a current-input hash after publication.
            "preserved_pin_seeds": {
                role: {"path": f"containers/{LOCK_FILES[role]}", "sha256": digest}
                for role, digest in preserved_pin_seeds.items()
            },
            "source_root": str(root),
            "maintenance_evidence": str(evidence / "dependency-refresh-evidence.json"),
            "consumed_source_sha256": input_hashes,
            "platform": {"os": platform.system().lower(), "architecture": platform.machine().lower()},
            "interpreters": {role: value["resolver"] for role, value in records.items()},
            "tool": {"pip_compile": "container:python -m piptools", "versions": tool_versions},
            "index_url": request.index_url,
            "roles": records,
        }
        publication = dict(staged)
        destinations = dict(targets)
        shared_inputs = {(root / "containers/resolver-tooling.lock").resolve(), Path(__file__).resolve(),
                         Path(__file__).with_name("dependency_locks.py").resolve()}
        for evidence_name in evidence_files_for(request.roles):
            # Only this file's own roles: their sources (plus the resolver tooling and the two maintenance
            # scripts every role consumes), their resolver records and their results. The full payload
            # above stays the outside-the-tree maintenance evidence.
            own = [role for role in request.roles if role in EVIDENCE_FILES[evidence_name]]
            own_inputs = set(shared_inputs)
            for role in own:
                own_inputs.update(path.resolve() for path in role_paths[role])
            own_payload = dict(evidence_payload)
            own_payload.update({
                "selected_roles": own,
                "consumed_source_sha256": {str(path): input_hashes[str(path)] for path in sorted(own_inputs)},
                "interpreters": {role: records[role]["resolver"] for role in own},
                "tool": {**evidence_payload["tool"], "versions": {role: tool_versions[role] for role in own}},
                "roles": {role: records[role] for role in own},
            })
            file_evidence = scratch / evidence_name
            file_evidence.write_text(json.dumps(own_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            publication[evidence_name] = file_evidence
            destinations[evidence_name] = containers / evidence_name
            print(f"dependency-refresh: evidence file={evidence_name} roles={own} inputs={len(own_inputs)}", flush=True)
        _publish_lock_set(publication, destinations, scratch / "lock-backups")
        evidence_tmp = evidence / f".dependency-refresh-{os.getpid()}.json"
        evidence_tmp.write_text(json.dumps(evidence_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(evidence_tmp, evidence / "dependency-refresh-evidence.json")
        # Confirm the committed outputs parse as the exact six-role set.
        load_dependency_locks(root, roles=request.roles)
        return RefreshResult(evidence / "dependency-refresh-evidence.json", targets, source.commit)
    finally:
        if runner is subprocess.run:
            try:
                inventory = _run(
                    runner,
                    (request.docker, "ps", "-aq", "--filter", f"label={resource_label}"),
                    cwd=root, env=_clean_environment(scratch), timeout=30,
                )
                for container_id in inventory.stdout.split():
                    if re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
                        _run(
                            runner, (request.docker, "rm", "-f", container_id),
                            cwd=root, env=_clean_environment(scratch), timeout=60,
                        )
            except RefreshError as exc:
                cleanup_error = f"owned resolver container cleanup failed: {exc}"
        try:
            if image_tags:
                _run(
                    runner,
                    (request.docker, "image", "rm", "--force", *image_tags.values()), cwd=root, env=_clean_environment(scratch), timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_error = str(exc)
        except RefreshError as exc:
            cleanup_error = str(exc)
        for temporary in atomic_temps:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink(missing_ok=True)
        if evidence_tmp is not None and (evidence_tmp.exists() or evidence_tmp.is_symlink()):
            evidence_tmp.unlink(missing_ok=True)
        ownership_error: RefreshError | None = None
        try:
            _verify_owned_scratch(scratch)
        except RefreshError as exc:
            ownership_error = exc
        shutil.rmtree(scratch, ignore_errors=False)
        if scratch.exists():
            raise RefreshError(f"refresh scratch cleanup failed: {scratch}")
        if ownership_error:
            raise ownership_error
        if cleanup_error:
            raise RefreshError(f"resolver image cleanup failed: {cleanup_error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--roles", default=",".join(LOCK_ROLES), help="Comma-separated source-owned roles to refresh")
    parser.add_argument("--preserve-existing-pins", action="store_true", help="Seed each selected resolver output with its current lock to retain compatible pins")
    parser.add_argument("--docker", default="docker", help="Docker executable (normally the code-owned Docker Engine CLI)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = refresh(
            RefreshRequest(
                source_root=Path(args.source_root),
                evidence_root=Path(args.evidence_root),
                index_url=args.index_url,
                docker=Path(args.docker),
                roles=tuple(args.roles.split(",")),
                preserve_existing_pins=args.preserve_existing_pins,
            )
        )
    except (RefreshError, OSError, DependencyLockError) as exc:
        print(f"dependency-refresh: {exc}", file=sys.stderr)
        return 78
    print(json.dumps({"evidence": str(result.evidence), "source_commit": result.source_commit, "locks": {role: str(path) for role, path in result.locks.items()}}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
