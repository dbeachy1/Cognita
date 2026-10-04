"""Static release checks for the reproducible NVIDIA image inputs (DESIGN-NVIDIA-ACCELERATION 5, 9, 13).

The twin of tests/test_amd_release_assets.py. Nothing here needs a GPU, Docker or the network, and
nothing waits on a clock: every check reads committed files or drives the manifest generator with
its two probes replaced by fixed values.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import scripts.verify_ocr_runtime as runtime_verify
from scripts.dependency_locks import (
    DependencyLockError,
    parse_hash_locked_requirements,
    validate_lock_role,
    validate_nvidia_ocr_lock,
    validate_runtime_lock,
)


ROOT = Path(__file__).parents[1]
NVIDIA = ROOT / "containers" / "cognita-nvidia"
DOCKERFILE = NVIDIA / "Dockerfile"
RUNTIME_LOCK = NVIDIA / "runtime-lock.json"
EMBED_LOCK = ROOT / "containers" / "embedding-nvidia-requirements.lock"
OCR_LOCK = ROOT / "containers" / "ocr-nvidia-requirements.lock"
CPU_BASE = "python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e"


def _sha256_text(path: Path) -> str:
    """Hash the canonical LF build-context bytes for a text artifact."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _lock_records(path: Path, role: str):
    return parse_hash_locked_requirements(path.read_text(encoding="utf-8"), role=role)


def test_nvidia_base_is_the_cpu_base_and_digest_qualified() -> None:
    dockerfile = _dockerfile()
    cpu = (ROOT / "containers" / "cognita" / "Dockerfile").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "containers" / "image-manifest.json").read_text(encoding="utf-8"))
    assert f"ARG PYTHON_BASE_IMAGE={CPU_BASE}" in dockerfile
    assert f"ARG PYTHON_BASE_IMAGE={CPU_BASE}" in cpu
    assert "FROM ${PYTHON_BASE_IMAGE} AS app" in dockerfile
    assert manifest["built"]["cognita_nvidia"]["base_reference"] == CPU_BASE == manifest["built"]["cognita"]["base_reference"]
    assert manifest["built"]["cognita_nvidia"]["dockerfile"] == "containers/cognita-nvidia/Dockerfile"
    assert not {"tag", "digest", "status"} & set(manifest["built"]["cognita_nvidia"])
    # One interpreter and one stage: no second base image, no copy of another image's /usr/local.
    assert len(re.findall(r"^FROM ", dockerfile, re.MULTILINE)) == 1
    assert "COPY --from" not in dockerfile


def test_nvidia_image_never_uses_a_cuda_base_or_a_devel_image_or_a_vendor_index() -> None:
    """The Deep Learning Container License stays out (a plain python base), CUDA developer tools are
    not redistributable, and nothing is fetched from anywhere but PyPI."""
    code = "\n".join(line for line in _dockerfile().splitlines() if not line.lstrip().startswith("#"))
    assert "nvidia/cuda" not in code and "-devel" not in code and "nvcr.io" not in code
    assert "--extra-index-url" not in code and ".whl" not in code and "download.pytorch.org" not in code
    urls = set(re.findall(r"https?://[^\s\"']+", code))
    assert urls == {"https://pypi.org/simple", "https://github.com/dbeachy1/Cognita"}


def test_nvidia_image_keeps_the_cpu_package_line_user_roots_and_entrypoint() -> None:
    dockerfile = _dockerfile()
    cpu = (ROOT / "containers" / "cognita" / "Dockerfile").read_text(encoding="utf-8")
    apt = "apt-get install --no-install-recommends -y ca-certificates libpq5 libgomp1"
    assert apt in dockerfile and apt in cpu
    assert "python3-venv" not in dockerfile
    for line in (
        "RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cognita",
        "mkdir -p /app/config /var/lib/cognita/models /var/lib/cognita/transfers",
        "USER cognita",
        "EXPOSE 8675 8676",
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["cognita"]',
        'CMD ["serve"]',
    ):
        assert line in dockerfile and line in cpu
    assert 'org.opencontainers.image.title="Cognita Knowledge gateway (NVIDIA)"' in dockerfile
    assert 'org.opencontainers.image.source="https://github.com/dbeachy1/Cognita"' in dockerfile


def test_nvidia_image_profile_environment_and_driver_capabilities() -> None:
    dockerfile = _dockerfile()
    for env in (
        "COGNITA_ACCELERATION_PROFILE=nvidia",
        "COGNITA_EMBED_RUNTIME=/opt/cognita-runtimes/embed/bin/python",
        "COGNITA_OCR_RUNTIME=/opt/cognita-runtimes/ocr/bin/python",
        "COGNITA_OCR_MODEL_DIR=/var/lib/cognita/models/easyocr",
        # libcuda (compute) and libnvidia-ml (utility, what the card probe reads) are injected by the toolkit.
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
    ):
        assert env in dockerfile
    # The device reservation, not the image, decides which cards a container sees.
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    assert "NVIDIA_VISIBLE_DEVICES" not in code


def test_nvidia_version_comes_from_the_build_argument_only() -> None:
    from cognita.release_identity import APPLICATION_VERSION

    dockerfile = _dockerfile()
    assert re.search(r"^ARG COGNITA_VERSION$", dockerfile, re.MULTILINE)
    assert re.search(r"^ARG COGNITA_COMMIT$", dockerfile, re.MULTILINE)
    assert 'org.opencontainers.image.version="${COGNITA_VERSION}"' in dockerfile
    assert 'org.opencontainers.image.revision="${COGNITA_COMMIT}"' in dockerfile
    assert APPLICATION_VERSION not in dockerfile
    assert APPLICATION_VERSION not in RUNTIME_LOCK.read_text(encoding="utf-8")
    assert APPLICATION_VERSION not in (ROOT / "compose.nvidia.yaml").read_text(encoding="utf-8")


def test_nvidia_runtimes_install_only_from_their_hash_locks() -> None:
    dockerfile = _dockerfile()
    flags = "--index-url https://pypi.org/simple --require-hashes --no-deps --only-binary=:all:"
    assert (
        f"/opt/cognita-runtimes/embed/bin/python -m pip install --no-cache-dir {flags} "
        "-r /opt/cognita-locks/embedding-nvidia-requirements.lock"
    ) in dockerfile
    assert (
        f"/opt/cognita-runtimes/ocr/bin/python -m pip install --no-cache-dir {flags} "
        "-r /opt/cognita-locks/ocr-nvidia-requirements.lock"
    ) in dockerfile
    assert "RUN python -m venv /opt/cognita-runtimes/embed" in dockerfile
    assert "RUN python -m venv /opt/cognita-runtimes/ocr" in dockerfile
    assert "COPY containers/embedding-nvidia-requirements.lock /opt/cognita-locks/embedding-nvidia-requirements.lock" in dockerfile
    assert "COPY containers/ocr-nvidia-requirements.lock /opt/cognita-locks/ocr-nvidia-requirements.lock" in dockerfile
    # Every pip install in the file is hash-locked or installs the checked-out package with no dependencies.
    for line in dockerfile.splitlines():
        if "pip install" in line and not line.lstrip().startswith("#"):
            assert "--require-hashes" in line or "install --no-cache-dir . --no-deps" in line, line


def test_nvidia_static_build_assertions_are_present_and_narrow() -> None:
    """DESIGN 5.2: no card on the build host, so the checks are static -- provider registered, pip check
    with exactly the named exemptions, and ldd resolving everything but the two driver sonames."""
    dockerfile = _dockerfile()
    assert "assert 'CUDAExecutionProvider' in providers" in dockerfile
    assert "requires onnxruntime, which is not installed." in dockerfile and "line.lower().startswith('fastembed ')" in dockerfile
    assert "requires triton, which is not installed." in dockerfile and "line.lower().startswith('torch ')" in dockerfile
    assert "('libcuda.so.1', 'libnvidia-ml.so.1')" in dockerfile
    assert "libonnxruntime_providers_cuda.so" in dockerfile
    assert "LD_LIBRARY_PATH=open('/opt/cognita-runtimes/embed/.ld_library_path')" in dockerfile
    assert "assert not unexpected" in dockerfile


def test_nvidia_image_writes_the_library_path_marker_before_the_ldd_check() -> None:
    """Without the marker CUDAExecutionProvider is 'available' but the live session silently runs on the
    CPU (DESIGN 1.3, 5.3). gpu_host reads the file at the venv root, unresolved."""
    dockerfile = _dockerfile()
    marker = dockerfile.index("open('/opt/cognita-runtimes/embed/.ld_library_path', 'w').write(':'.join(dirs))")
    assert "os.path.join(site, 'nvidia', '*', 'lib')" in dockerfile
    assert "assert dirs" in dockerfile
    assert marker < dockerfile.index("libonnxruntime_providers_cuda.so")
    # The parent's reader and the image agree on the file's name and place.
    source = (ROOT / "src" / "cognita" / "gpu_host.py").read_text(encoding="utf-8")
    assert 'root / ".ld_library_path"' in source
    assert "Path(venv_python).parent.parent" in source


def test_nvidia_ocr_manifest_is_generated_at_build_from_the_nvidia_lock() -> None:
    dockerfile = _dockerfile()
    generate = dockerfile[dockerfile.index("--generate-manifest"):]
    generate = generate[:generate.index("\n\n")]
    assert "--model-dir" not in generate
    assert "--runtime-lock /opt/cognita-runtimes/runtime-lock.json" in generate
    assert "--requirements-lock /opt/cognita-locks/ocr-nvidia-requirements.lock" in generate
    assert "--model-source-manifest /opt/cognita-locks/easyocr-qualification-dependencies.json" in generate
    assert "--manifest /opt/cognita-models/easyocr-qualification.json" in generate
    assert "cognita-amd" not in dockerfile and "*.pth" not in dockerfile
    assert "COPY containers/cognita-nvidia/runtime-lock.json /opt/cognita-runtimes/runtime-lock.json" in dockerfile
    assert "scripts/nvidia_runtime_preflight.py" in dockerfile and "amd_runtime_preflight" not in dockerfile


def test_nvidia_expensive_layers_precede_source_and_version_layers() -> None:
    dockerfile = _dockerfile()
    ownership_setup = dockerfile.index("RUN useradd --create-home --uid 10001")
    embedding_install = dockerfile.index("RUN python -m venv /opt/cognita-runtimes/embed")
    marker = dockerfile.index("open('/opt/cognita-runtimes/embed/.ld_library_path', 'w')")
    ocr_install = dockerfile.index("RUN python -m venv /opt/cognita-runtimes/ocr")
    manifest = dockerfile.index("--generate-manifest")
    service_install = dockerfile.index(
        "RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple "
        "--require-hashes --no-deps -r /opt/cognita-locks/build-requirements.lock"
    )
    source_copy = dockerfile.index("COPY pyproject.toml README.md ./")
    version_arg = dockerfile.index("ARG COGNITA_VERSION")
    label = dockerfile.index('LABEL org.opencontainers.image.title="Cognita Knowledge gateway (NVIDIA)"')
    assert ownership_setup < embedding_install < marker < ocr_install < manifest
    assert manifest < service_install < source_copy < version_arg < label
    assert "chown -R cognita:cognita /app /opt/cognita-runtimes" not in dockerfile


def test_nvidia_wheels_keep_their_license_files() -> None:
    """DESIGN 13: the image carries NVIDIA's own license files; nothing may strip them."""
    code = "\n".join(line for line in _dockerfile().splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"rm\s+-[a-z]*\s+[^\n]*(LICENSE|dist-info|\.dist-info)", code, re.IGNORECASE)
    assert "find " not in code and "-delete" not in code


def test_nvidia_locks_are_hash_pinned_pypi_only_and_role_valid() -> None:
    for path, role in ((EMBED_LOCK, "embedding-nvidia"), (OCR_LOCK, "ocr-nvidia")):
        records = _lock_records(path, role)
        validate_lock_role(records, role=role)
        assert len(records) > 20, role
        for item in records:
            assert not item.version.startswith("@ "), f"{role}: {item.name} is a direct URL, not PyPI"
            assert item.hashes and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in item.hashes)
        text = path.read_text(encoding="utf-8")
        assert "--index-url" not in text and "--extra-index-url" not in text and not re.search(r"https?://", text)


def test_embedding_nvidia_lock_pins_the_cuda_13_provider_and_its_libraries() -> None:
    records = {item.name: item.version for item in _lock_records(EMBED_LOCK, "embedding-nvidia")}
    lock = json.loads(RUNTIME_LOCK.read_text(encoding="utf-8"))["embedding"]
    assert records["fastembed"] == lock["fastembed"] == "0.8.0"
    assert records["onnxruntime-gpu"] == lock["onnxruntime-gpu"] == "1.30.0"
    assert "onnxruntime" not in records and "onnxruntime-migraphx" not in records
    # The runtime lock's NVIDIA wheel table is the lock's own, name for name and version for version,
    # and the table is every nvidia-* distribution in the lock.
    assert lock["nvidia_wheels"] == {name: version for name, version in records.items() if name.startswith("nvidia-")}
    assert {"nvidia-cuda-runtime", "nvidia-cublas", "nvidia-cufft", "nvidia-curand", "nvidia-cuda-nvrtc",
            "nvidia-nvjitlink", "nvidia-cudnn-cu13"} <= set(lock["nvidia_wheels"])
    assert not [name for name in lock["nvidia_wheels"] if name.endswith(("-cu11", "-cu12"))]


def test_ocr_nvidia_lock_has_the_cuda_torch_and_no_triton() -> None:
    records = {item.name: item.version for item in _lock_records(OCR_LOCK, "ocr-nvidia")}
    ocr = json.loads(RUNTIME_LOCK.read_text(encoding="utf-8"))["ocr"]
    assert (records["easyocr"], records["torch"], records["torchvision"]) == ("1.7.2", "2.14.0", "0.29.0")
    assert "+" not in records["torch"]  # PyPI carries the plain version; the local +cu130 is the wheel's own
    assert not [name for name in records if name.startswith(("triton", "rocm"))]
    assert "nvidia-cudnn-cu13" in records and "cuda-toolkit" in records
    for package in ("Pillow", "opencv-python-headless", "numpy", "psutil"):
        assert records[package.lower()] == ocr[package]
    assert validate_nvidia_ocr_lock(RUNTIME_LOCK, OCR_LOCK)["torch"] == "2.14.0"
    assert ocr["triton"] == "excluded"


def test_nvidia_runtime_lock_covers_source_hashes() -> None:
    lock = json.loads(RUNTIME_LOCK.read_text(encoding="utf-8"))
    validate_runtime_lock(RUNTIME_LOCK, repo_root=ROOT)
    artifacts = lock["artifacts"]
    assert artifacts["preflight_source"] == {
        "path": "scripts/nvidia_runtime_preflight.py",
        "sha256": _sha256_text(ROOT / "scripts" / "nvidia_runtime_preflight.py"),
    }
    assert artifacts["dockerfile_source"] == {
        "path": "containers/cognita-nvidia/Dockerfile",
        "sha256": _sha256_text(DOCKERFILE),
    }
    assert lock["driver_floor"] == {"linux": "580", "windows_wsl": "580"}
    assert lock["model_source_manifest"] == "docs/easyocr-qualification-dependencies.json"
    assert lock["runtime_paths"]["ocr_models"] == "/var/lib/cognita/models/easyocr"


def test_nvidia_text_hash_contract_is_crlf_stable() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "artifact.txt"
        path.write_bytes(b"alpha\r\nbeta\r\n")
        crlf = _sha256_text(path)
        path.write_bytes(b"alpha\nbeta\n")
        assert _sha256_text(path) == crlf


def test_nvidia_hash_inputs_have_platform_stable_git_attributes() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/nvidia_runtime_preflight.py text eol=lf" in attributes
    assert "containers/cognita-nvidia/runtime-lock.json text eol=lf" in attributes
    assert "containers/cognita-nvidia/Dockerfile text eol=lf" in attributes


def test_image_manifest_records_the_nvidia_inputs_and_their_lock_hashes() -> None:
    entry = json.loads((ROOT / "containers" / "image-manifest.json").read_text(encoding="utf-8"))["built"]["cognita_nvidia"]
    assert entry["context"] == "."
    assert entry["embedding_python"] == "base:/usr/local/bin/python (cp313)"
    for key, path in (("embedding_lock", EMBED_LOCK), ("ocr_lock", OCR_LOCK)):
        assert entry[key] == {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256_text(path)}
    # No provider wheel URL and no deb: PyPI is the only source.
    assert not {"embedding_provider_wheel", "embedding_migraphx_deb", "python_runtime_reference"} & set(entry)


def test_committed_nvidia_evidence_matches_the_committed_inputs() -> None:
    """The lock refresh is the only way the locks move, and its evidence binds them to the exact source
    files it consumed. An edit to any of those files without a fresh
    `refresh_dependency_locks.py --roles embedding-nvidia,ocr-nvidia` fails here, not on kei."""
    evidence = json.loads((ROOT / "containers" / "nvidia-refresh-evidence.json").read_text(encoding="utf-8"))
    assert {"embedding-nvidia", "ocr-nvidia"} <= set(evidence["roles"])
    assert evidence["index_url"] == "https://pypi.org/simple"
    for role, path in (("embedding-nvidia", EMBED_LOCK), ("ocr-nvidia", OCR_LOCK)):
        record = evidence["roles"][role]
        assert record["lock_sha256"] == _sha256_text(path)
        assert record["packages"] == len(_lock_records(path, role))
        assert record["resolver"]["indexes"] == ["https://pypi.org/simple"]
        assert record["resolver"]["expected_python"] == "3.13" and record["resolver"]["vendor_artifacts"] == {}
        assert record["resolver"]["image"] == CPU_BASE
    source_root = Path(evidence["source_root"])
    for absolute, digest in evidence["consumed_source_sha256"].items():
        target = ROOT / Path(absolute).relative_to(source_root)
        assert target.is_file() and not target.is_symlink(), target
        assert _sha256_text(target) == digest, f"{target.relative_to(ROOT).as_posix()} changed since the refresh"


def test_nvidia_overlay_keeps_gpu_devices_off_workspace_runtime_and_sets_the_profile() -> None:
    base = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    text = (ROOT / "compose.nvidia.yaml").read_text(encoding="utf-8")
    overlay = yaml.safe_load(text)
    service = overlay["services"]["cognita"]
    assert set(overlay["services"]) == {"cognita"}
    assert service["environment"] == {"COGNITA_ACCELERATION_PROFILE": "nvidia"}
    assert service["build"]["dockerfile"] == "containers/cognita-nvidia/Dockerfile"
    assert service["image"] == "cognita-nvidia:${COGNITA_VERSION:?COGNITA_VERSION is required}"
    assert service["build"]["args"]["COGNITA_VERSION"] == "${COGNITA_VERSION:?COGNITA_VERSION is required}"
    # All cards, chosen per card in Admin: the reservation names the driver and every device.
    assert service["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    ]
    for absent in ("devices", "group_add", "privileged", "runtime"):
        assert absent not in service
    assert "workspace-runtime" not in text and "broker" not in text.lower().replace("the broker never", "")
    assert "COGNITA_GPU_ENABLED" not in base + text and "COGNITA_OCR_DEVICE" not in base + text
    assert "COGNITA_GPU_PROGRAM_CACHE_DIR" not in text and "NVIDIA_VISIBLE_DEVICES" not in text


def test_cpu_image_test_stage_copies_the_nvidia_overlay_by_name() -> None:
    """publish's in-image suite reads the Compose files from the test stage, which copies them by name."""
    cpu = (ROOT / "containers" / "cognita" / "Dockerfile").read_text(encoding="utf-8")
    copy = cpu[cpu.index("COPY compose.yaml"):]
    copy = copy[:copy.index("/opt/cognita-tests/\n")]
    for name in ("compose.yaml", "compose.amd.yaml", "compose.nvidia.yaml", "compose.cpu.yaml", "compose.workspace.yaml"):
        assert name in copy.split()


def test_nvidia_preflight_performs_live_session_canaries_and_cleanup() -> None:
    source = (ROOT / "scripts" / "nvidia_runtime_preflight.py").read_text(encoding="utf-8")
    assert '"CUDAExecutionProvider"' in source and "CPUExecutionProvider" in source
    assert "TextEmbedding" in source and "OCRWorkerRunner" in source and "asyncio.run" in source
    assert "python=sys.executable" in source and "gc.collect()" in source
    assert "verification_timeout" in source and "driver_too_old" in source
    assert 'os.environ["CUDA_VISIBLE_DEVICES"]' in source and 'env["CUDA_VISIBLE_DEVICES"]' in source
    assert "_require_bound_pci(card)" in source
    # No program cache and no AMD names: those are MIGraphX-only (DESIGN 3, 5.7).
    assert "program_cache" not in source.lower() and "ROCR_VISIBLE_DEVICES" not in source and "MIGraphX" not in source
    assert "import amd_runtime_preflight" not in source and "from amd_runtime_preflight" not in source


def test_nvidia_preflight_requires_the_ocr_weights_only_for_the_ocr_component() -> None:
    source = (ROOT / "scripts" / "nvidia_runtime_preflight.py").read_text(encoding="utf-8")
    embed = source[source.index("def _embed("):source.index("def _ocr(")]
    ocr = source[source.index("def _ocr("):source.index("def main(")]
    assert "_load_lock(models=False)" in embed
    assert "_load_lock()" in ocr


def test_nvidia_preflight_categories_are_bounded_and_child_codes_are_distinct() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("nvidia_runtime_preflight", ROOT / "scripts" / "nvidia_runtime_preflight.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "driver_too_old" in module._CATEGORIES
    assert len(set(module._CHILD_FAILURE_CODES.values())) == len(module._CATEGORIES)
    assert all(code >= 20 for code in module._CHILD_FAILURE_CODES.values())
    # The AMD script (not touched by this change) has no driver_too_old.
    amd = (ROOT / "scripts" / "amd_runtime_preflight.py").read_text(encoding="utf-8")
    assert "driver_too_old" not in amd


# ---------------------------------------------------------------------------------------------------
# The OCR qualification manifest generator on the NVIDIA runtime lock (scripts/verify_ocr_runtime.py).
# The probes are replaced by fixed values, so no interpreter, torch or network is involved.
# ---------------------------------------------------------------------------------------------------

_RUNTIME_NAMES = ("easyocr", *runtime_verify._RUNTIME_PACKAGES)


@pytest.fixture
def nvidia_manifest_inputs(tmp_path, monkeypatch):
    declaration = tmp_path / "runtime-lock.json"
    declaration.write_bytes(RUNTIME_LOCK.read_bytes())
    lock = tmp_path / "ocr-nvidia.lock"
    lock.write_bytes(OCR_LOCK.read_bytes())
    expected = validate_nvidia_ocr_lock(declaration, lock)
    hashes = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("craft_mlt_25k.pth", "english_g2.pth")}
    aggregate = hashlib.sha256("".join(hashes.values()).encode()).hexdigest()
    model_source = tmp_path / "easyocr-qualification-dependencies.json"
    model_source.write_text(json.dumps({"model_files": {**hashes, "aggregate_sha256": aggregate}}), encoding="utf-8")
    # What the OCR interpreter reports: torch.__version__ carries the CUDA local version, the rest is plain.
    imported = {name: expected[name.lower()] for name in _RUNTIME_NAMES}
    imported["torch"] = expected["torch"] + "+cu130"
    monkeypatch.setattr(runtime_verify, "_probe", lambda python: dict(imported))
    monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
        "versions": dict(expected), "python": "3.13.12", "platform": "Linux-x86_64", "libc": ["glibc", "2.36"]})
    return SimpleNamespace(declaration=declaration, lock=lock, model_source=model_source,
                           expected=expected, imported=imported, tmp_path=tmp_path)


def _generate(inputs, name="qualified.json"):
    output = inputs.tmp_path / name
    manifest = runtime_verify.generate_manifest(
        Path(sys.executable), output, runtime_lock=inputs.declaration,
        requirements_lock=inputs.lock, model_source_manifest=inputs.model_source)
    return output, manifest


def test_nvidia_manifest_records_the_imported_strings_and_is_accepted_by_the_worker(nvidia_manifest_inputs, monkeypatch) -> None:
    import importlib.metadata

    from cognita.assets.ocr_service import configured_pipeline_identity
    from cognita.assets.ocr_worker import _verify_qualification

    inputs = nvidia_manifest_inputs
    output, manifest = _generate(inputs)
    assert set(manifest["runtime"]) == set(runtime_verify._RUNTIME_PACKAGES)
    assert manifest["runtime"]["torch"] == "2.14.0+cu130"
    assert manifest["runtime"]["torchvision"] == "0.29.0"
    assert manifest["engine"] == "easyocr==1.7.2"
    assert configured_pipeline_identity(SimpleNamespace(ocr_qualification_manifest=output), ("en",))
    # The worker compares torch.__version__ as is and everything else through the metadata, unchanged.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: inputs.expected[name.lower()])
    models = inputs.tmp_path / "models"
    models.mkdir()
    for name in ("craft_mlt_25k.pth", "english_g2.pth"):
        (models / name).write_bytes(name.encode())
    easyocr = SimpleNamespace(__version__=inputs.expected["easyocr"])
    assert _verify_qualification(str(models), str(output), easyocr, SimpleNamespace(__version__="2.14.0+cu130"))
    assert not _verify_qualification(str(models), str(output), easyocr, SimpleNamespace(__version__="2.14.0"))
    assert not list(inputs.tmp_path.glob(".cognita-ocr-manifest-*"))


def test_nvidia_manifest_accepts_the_declared_cuda_suffix_on_torch_and_torchvision(nvidia_manifest_inputs, monkeypatch) -> None:
    inputs = nvidia_manifest_inputs
    imported = dict(inputs.imported)
    imported["torchvision"] = inputs.expected["torchvision"] + "+cu130"
    monkeypatch.setattr(runtime_verify, "_probe", lambda python: dict(imported))
    _output, manifest = _generate(inputs)
    assert manifest["runtime"]["torch"] == "2.14.0+cu130" and manifest["runtime"]["torchvision"] == "0.29.0+cu130"
    # A plain torch (no local version) is also the locked one and is accepted.
    imported["torch"] = inputs.expected["torch"]
    _output, manifest = _generate(inputs, "plain.json")
    assert manifest["runtime"]["torch"] == "2.14.0"


@pytest.mark.parametrize(
    "package, imported",
    [
        ("torch", "2.14.0+cu129"),
        ("torch", "2.14.0+cu12"),
        ("torch", "2.14.0+rocm7.1"),
        ("torch", "2.14.0+cpu"),
        ("torch", "2.14.0+"),
        ("torch", "2.14.0+cu130.dev1"),
        ("torch", "2.14.1+cu130"),
        ("torch", "2.13.0"),
        ("torchvision", "0.29.0+rocm7.1"),
        ("torchvision", "0.29.1"),
        ("numpy", "2.2.6+cu130"),
        ("Pillow", "11.3.0+cu130"),
        ("easyocr", "1.7.2+cu130"),
        ("psutil", "7.0.1"),
    ],
)
def test_nvidia_manifest_refuses_any_other_suffix_or_a_base_mismatch(nvidia_manifest_inputs, monkeypatch, package, imported) -> None:
    inputs = nvidia_manifest_inputs
    values = dict(inputs.imported)
    values[package] = imported
    monkeypatch.setattr(runtime_verify, "_probe", lambda python: dict(values))
    with pytest.raises(RuntimeError, match="NVIDIA locked closure"):
        _generate(inputs)
    assert not (inputs.tmp_path / "qualified.json").exists()
    assert not list(inputs.tmp_path.glob(".cognita-ocr-manifest-*"))


def test_nvidia_suffix_comes_from_the_runtime_lock_not_from_the_imported_string(nvidia_manifest_inputs, monkeypatch) -> None:
    """cuda_major 13 means +cu130 and only that; the lock decides."""
    inputs = nvidia_manifest_inputs
    value = json.loads(inputs.declaration.read_text(encoding="utf-8"))
    assert runtime_verify._declared_cuda_major(inputs.declaration) == value["ocr"]["cuda_major"] == 13
    assert runtime_verify._nvidia_version_matches("torch", "2.14.0+cu130", "2.14.0", "+cu130")
    assert not runtime_verify._nvidia_version_matches("torch", "2.14.0+cu131", "2.14.0", "+cu130")
    assert not runtime_verify._nvidia_version_matches("torch", "2.14.0+cu130+cu130", "2.14.0", "+cu130")
    assert not runtime_verify._nvidia_version_matches("numpy", "2.2.6+cu130", "2.2.6", "+cu130")


def test_nvidia_manifest_refuses_a_closure_or_source_that_disagrees(nvidia_manifest_inputs, monkeypatch) -> None:
    inputs = nvidia_manifest_inputs
    monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
        "versions": {**inputs.expected, "easyocr": "unexpected"}, "python": "3.13.12"})
    with pytest.raises(RuntimeError, match="OCR installed closure differs from the NVIDIA source/lock"):
        _generate(inputs)
    monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
        "versions": dict(inputs.expected), "python": "3.12.3"})
    with pytest.raises(RuntimeError, match="NVIDIA source/lock"):
        _generate(inputs)
    monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
        "versions": dict(inputs.expected), "python": "3.13.12"})
    value = json.loads(inputs.declaration.read_text(encoding="utf-8"))
    value["model_source_manifest"] = "docs/some-other-manifest.json"
    inputs.declaration.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError, match="NVIDIA model source manifest differs"):
        _generate(inputs)
    assert not (inputs.tmp_path / "qualified.json").exists()


def test_nvidia_manifest_refuses_a_lock_that_disagrees_with_the_runtime_lock(nvidia_manifest_inputs) -> None:
    inputs = nvidia_manifest_inputs
    inputs.lock.write_text(inputs.lock.read_text(encoding="utf-8").replace("easyocr==1.7.2", "easyocr==1.7.1", 1), encoding="utf-8")
    with pytest.raises(DependencyLockError, match="version mismatch: easyocr"):
        _generate(inputs)
    assert not (inputs.tmp_path / "qualified.json").exists()


def test_the_cpu_manifest_path_is_unchanged_by_the_nvidia_branch() -> None:
    """The CPU runtime lock declares no cuda_major, so generation keeps taking the CPU path and its messages."""
    cpu_lock = ROOT / "containers" / "cognita" / "runtime-lock.json"
    assert runtime_verify._declared_cuda_major(cpu_lock) is None
    assert runtime_verify._declared_cuda_major(ROOT / "containers" / "cognita-amd" / "runtime-lock.json") is None
    assert runtime_verify._declared_cuda_major(ROOT / "does-not-exist.json") is None
    # ... and the CPU error text is what it always was (the label only changes on the NVIDIA branch).
    with pytest.raises(RuntimeError, match="CPU model source manifest differs from the declaration"):
        runtime_verify.generate_manifest(
            Path(sys.executable), Path("never-written.json"), runtime_lock=cpu_lock,
            requirements_lock=ROOT / "containers" / "ocr-cpu-requirements.lock",
            model_source_manifest=Path("some-other-manifest.json"))
    assert not Path("never-written.json").exists()
