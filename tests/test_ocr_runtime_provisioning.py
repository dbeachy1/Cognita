"""Dedicated OCR interpreter/model provisioning contracts."""

from __future__ import annotations

import hashlib
import json
import sys
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.verify_ocr_runtime as runtime_verify
from cognita.assets.ocr_worker import (
    OCRWorkerError,
    OCRWorkerRunner,
    _application_source_root,
    _safe_env,
)
from cognita.config import CognitaConfig
from scripts.verify_ocr_runtime import verify


def test_runner_never_falls_back_to_service_interpreter():
    runner = OCRWorkerRunner(SimpleNamespace(ocr_model_dir=""))
    assert runner.python == ""


@pytest.mark.parametrize("failure", [False, True])
def test_probe_cache_does_not_require_username_and_is_removed(monkeypatch, failure):
    monkeypatch.setenv("USER", "must-not-cross-probe-boundary")
    monkeypatch.setenv("LOGNAME", "must-not-cross-probe-boundary")
    observed = []
    def execute(command, *, env, **kwargs):
        assert "USER" not in env and "LOGNAME" not in env
        root = Path(env["HOME"])
        observed.append(root)
        assert root.is_dir()
        for key in ("TORCHINDUCTOR_CACHE_DIR", "TORCH_HOME", "EASYOCR_MODULE_PATH", "XDG_CACHE_HOME"):
            target = Path(env[key])
            assert target.is_relative_to(root)
            target.mkdir()
            (target / "owned-cache").write_bytes(b"synthetic")
        if failure:
            raise subprocess.CalledProcessError(1, command, stderr="synthetic import failure")
        return subprocess.CompletedProcess(command, 0, "1.7.2|2.10.0+cpu|0.25.0+cpu|11.3.0|4.12.0.88|2.2.6|7.0.0", "")
    monkeypatch.setattr(runtime_verify.subprocess, "run", execute)
    if failure:
        with pytest.raises(RuntimeError, match="version probe failed"):
            runtime_verify._probe(Path(sys.executable))
    else:
        assert runtime_verify._probe(Path(sys.executable))["torch"] == "2.10.0+cpu"
    assert observed and all(not path.exists() for path in observed)


@pytest.mark.asyncio
async def test_worker_acquisition_failure_removes_cache_without_forwarding_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "must-not-cross-worker-boundary")
    monkeypatch.setenv("LOGNAME", "must-not-cross-worker-boundary")
    monkeypatch.setenv("SECRET_TEST_TOKEN", "must-not-cross-worker-boundary")
    observed = []
    async def fail(*command, env, **kwargs):
        assert all(key not in env for key in ("USER", "LOGNAME", "SECRET_TEST_TOKEN"))
        root = Path(env["HOME"])
        assert root.is_dir()
        assert Path(env["TORCHINDUCTOR_CACHE_DIR"]).is_relative_to(root)
        observed.append(root)
        raise OSError("synthetic launch failure")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    worker = OCRWorkerRunner(python=sys.executable, model_dir=tmp_path)
    with pytest.raises(OSError, match="launch failure"):
        await worker.run(b"synthetic", ("en",))
    assert observed and not observed[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_worker_cache_lifetime_covers_execution_and_teardown(tmp_path, monkeypatch, outcome):
    worker = OCRWorkerRunner(python=sys.executable, model_dir=tmp_path)
    observed = []
    result = object()
    async def execute(request, *, cache_root, **kwargs):
        observed.append(cache_root)
        env = worker._worker_env(cache_root=cache_root)
        assert cache_root.is_dir()
        Path(env["TORCHINDUCTOR_CACHE_DIR"]).mkdir()
        if outcome == "failure":
            raise OCRWorkerError("ocr_unavailable")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(worker, "_run_framed", execute)
    if outcome == "success":
        assert await worker.run(b"synthetic", ("en",)) is result
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else OCRWorkerError):
            await worker.run(b"synthetic", ("en",))
    assert len(observed) == 1 and not observed[0].exists()


def test_config_requires_absolute_dedicated_interpreter_path():
    with pytest.raises(ValueError, match="absolute path"):
        CognitaConfig(ocr_python="python")


def test_config_validates_ordered_ocr_gpu_uuid_pins():
    config = CognitaConfig(ocr_gpu_device_ids=["GPU-a1", "GPU-b2"])
    assert config.ocr_gpu_device_ids == ["GPU-a1", "GPU-b2"]
    with pytest.raises(ValueError, match="unique UUIDs"):
        CognitaConfig(ocr_gpu_device_ids=["GPU-a1", "GPU-a1"])
    with pytest.raises(ValueError, match="GPU-<hex UUID>"):
        CognitaConfig(ocr_gpu_device_ids=["card1"])
    # 15.0: NVIDIA's NVML UUID is dashed; the old check refused it.
    nvidia = "GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
    assert CognitaConfig(ocr_gpu_device_ids=[nvidia]).ocr_gpu_device_ids == [nvidia]
    for bad in ("GPU-", "GPU-xyz", "GPU-4cd2--e5a4", "GPU-4cd2-", "gpu0"):
        with pytest.raises(ValueError, match="GPU-<hex UUID>"):
            CognitaConfig(ocr_gpu_device_ids=[bad])
    with pytest.raises(ValueError, match="only one"):
        CognitaConfig(
            ocr_gpu_device_ids=["GPU-a1"], ocr_gpu_device_id="GPU-b2",
        )


@pytest.mark.asyncio
async def test_missing_dedicated_runtime_or_model_is_unavailable(tmp_path):
    runtime = tmp_path / "ocr-python"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    runner = OCRWorkerRunner(SimpleNamespace(ocr_model_dir=str(model_dir)), python=str(runtime))
    with pytest.raises(OCRWorkerError) as error:
        await runner.run(b"PNG", ("en",))
    assert error.value.reason == "ocr_unavailable"


def test_offline_worker_environment_has_no_proxy_and_writable_easyocr_cache(monkeypatch):
    monkeypatch.setenv("HOME", "/var/lib/cognita/models")
    env = _safe_env("/srv/cognita/ocr-models", "GPU-test")
    assert env["NO_PROXY"] == env["no_proxy"] == "*"
    assert env["EASYOCR_MODULE_PATH"] == str(Path("/var/lib/cognita/models") / ".EasyOCR")
    assert env["EASYOCR_MODULE_PATH"] != "/srv/cognita/ocr-models"
    assert "PYTHONPATH" in env
    assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
    assert env["ROCR_VISIBLE_DEVICES"] == "GPU-test"
    assert env["HIP_VISIBLE_DEVICES"] == env["CUDA_VISIBLE_DEVICES"] == "GPU-test"


def test_offline_worker_environment_prefers_retained_image_source_over_site_packages(tmp_path):
    service_module = tmp_path / "site-packages" / "cognita" / "assets" / "ocr_worker.py"
    service_module.parent.mkdir(parents=True)
    service_module.write_text("", encoding="utf-8")
    image_source = tmp_path / "app" / "src"
    image_module = image_source / "cognita" / "assets" / "ocr_worker.py"
    image_module.parent.mkdir(parents=True)
    image_module.write_text("", encoding="utf-8")

    assert _application_source_root(service_module, packaged_root=image_source) == image_source


def test_worker_environment_uses_each_attempt_uuid_not_fixed_legacy_pin():
    runner = OCRWorkerRunner(
        SimpleNamespace(ocr_model_dir="/srv/cognita/ocr-models"),
        device_id="GPU-legacy",
    )
    first = runner._worker_env("GPU-a1")
    second = runner._worker_env("GPU-b2")
    cpu = runner._worker_env(None)
    assert first["ROCR_VISIBLE_DEVICES"] == "GPU-a1"
    assert second["ROCR_VISIBLE_DEVICES"] == "GPU-b2"
    assert "ROCR_VISIBLE_DEVICES" not in cpu


def test_provisioner_uses_qualified_index_and_verified_model_sources():
    script = Path("scripts/provision_ocr_runtime.sh").read_text(encoding="utf-8")
    assert "https://download.pytorch.org/whl/test/rocm7.1" in script
    assert "--index-strategy unsafe-best-match" in script
    assert "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip" in script
    assert "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip" in script
    assert "SHA-256 mismatch" in script
    assert "os.replace(temporary, destination)" in script


def test_verifier_preserves_dedicated_venv_launcher_path():
    script = Path("scripts/verify_ocr_runtime.py").read_text(encoding="utf-8")
    assert "args.python.absolute()" in script
    assert "args.python.resolve()" not in script


def test_systemd_launcher_receives_only_sanitized_worker_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/var/lib/cognita/models")
    runner = OCRWorkerRunner(
        SimpleNamespace(ocr_python="/opt/ocr/bin/python", ocr_model_dir="/opt/models"),
        device_id="GPU-test",
    )
    env = _safe_env(runner.model_dir, runner.device_id)
    command = ["systemd-run", "--user"]
    command.extend(
        f"--setenv={key}={env[key]}"
        for key in ("PYTHONPATH", "EASYOCR_MODULE_PATH", "PYTHONNOUSERSITE", "NO_PROXY", "no_proxy", "ROCR_VISIBLE_DEVICES")
        if key in env
    )
    assert "--setenv=PYTHONPATH=/opt/models" not in command
    assert f"--setenv=EASYOCR_MODULE_PATH={Path('/var/lib/cognita/models') / '.EasyOCR'}" in command
    assert "--setenv=ROCR_VISIBLE_DEVICES=GPU-test" in command


def test_provisioning_verifier_rejects_model_checksum_change(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model = model_dir / "english_g2.pth"
    model.write_bytes(b"synthetic-qualified-model")
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    runtime = {name: "qualified-test" for name in
               ("easyocr", "torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil")}
    aggregate = hashlib.sha256(model_hash.encode()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "runtime": runtime,
        "model_files": {"aggregate_sha256": aggregate, "english_g2.pth": model_hash},
    }), encoding="utf-8")
    original_probe = runtime_verify._probe
    runtime_verify._probe = lambda _python: runtime
    try:
        verify(Path(sys.executable), model_dir, manifest)
        model.write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="aggregate SHA-256"):
            verify(Path(sys.executable), model_dir, manifest)
    finally:
        runtime_verify._probe = original_probe


@pytest.fixture
def cpu_manifest_inputs(tmp_path, monkeypatch):
    from scripts.dependency_locks import cpu_ocr_declaration
    declaration = tmp_path / "runtime.json"
    declaration.write_bytes(Path("containers/cognita/runtime-lock.json").read_bytes())
    expected, vendors = cpu_ocr_declaration(declaration)
    requirements = json.loads(declaration.read_text())["requirements"]
    lock = tmp_path / "cpu.lock"
    lock.write_text("\n".join(item + " --hash=sha256:" + (item.split("#sha256=")[-1] if "#sha256=" in item else "a" * 64) for item in requirements) + "\n")
    # 14.2.0: manifest generation takes model_files from the source authority and reads no model bytes,
    # so the fixture has no model directory at all: only the two hashes and their aggregate.
    hashes = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("craft_mlt_25k.pth", "english_g2.pth")}
    aggregate = hashlib.sha256("".join(hashes.values()).encode()).hexdigest()
    model_source = tmp_path / "easyocr-qualification-dependencies.json"
    model_source.write_text(json.dumps({"model_files": {**hashes, "aggregate_sha256": aggregate}}))
    versions = {name: expected[name.lower()] for name in ("easyocr", *runtime_verify._RUNTIME_PACKAGES)}
    monkeypatch.setattr(runtime_verify, "_probe", lambda python: versions)
    monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
        "versions": expected, "python": "3.13.12", "platform": "Linux-x86_64", "libc": ["glibc", "2.36"]})
    return declaration, lock, model_source, expected


def _downloaded_models(tmp_path, model_source):
    """A model directory as ocr_weights.fetch leaves it: exactly the two files whose hashes the source lists.

    The fixture's "weights" are the file names' own bytes, so their hashes are the source authority's."""
    models = tmp_path / "downloaded-models"
    models.mkdir()
    for name in json.loads(model_source.read_text())["model_files"]:
        if name.endswith(".pth"):
            (models / name).write_bytes(name.encode())
    return models


def test_cpu_manifest_accepted_by_unchanged_pipeline_and_worker(cpu_manifest_inputs, tmp_path, monkeypatch):
    from cognita.assets.ocr_service import configured_pipeline_identity
    from cognita.assets.ocr_worker import _verify_qualification
    import importlib.metadata
    declaration, lock, model_source, expected = cpu_manifest_inputs
    output = tmp_path / "qualified.json"
    manifest = runtime_verify.generate_manifest(Path(sys.executable), output,
        runtime_lock=declaration, requirements_lock=lock, model_source_manifest=model_source)
    assert set(manifest["runtime"]) == set(runtime_verify._RUNTIME_PACKAGES)
    assert manifest["runtime"]["torch"].endswith("+cpu")
    # model_files and the aggregate are the source authority's, written without reading any model file.
    source_models = json.loads(model_source.read_text())["model_files"]
    assert manifest["model_files"] == source_models
    assert configured_pipeline_identity(SimpleNamespace(ocr_qualification_manifest=output), ("en",))
    # The worker's run-time check, unchanged, accepts a directory holding exactly the downloaded files ...
    monkeypatch.setattr(importlib.metadata, "version", lambda name: expected[name.lower()])
    models = _downloaded_models(tmp_path, model_source)
    easyocr, torch = SimpleNamespace(__version__=expected["easyocr"]), SimpleNamespace(__version__=expected["torch"])
    assert _verify_qualification(str(models), str(output), easyocr, torch)
    # ... and still refuses a wrong file or a missing one (hash checks unchanged).
    (models / "english_g2.pth").write_bytes(b"wrong bytes")
    assert not _verify_qualification(str(models), str(output), easyocr, torch)
    (models / "english_g2.pth").unlink()
    assert not _verify_qualification(str(models), str(output), easyocr, torch)
    assert not list(tmp_path.glob(".cognita-ocr-manifest-*"))


def test_cpu_generation_reads_no_model_file(cpu_manifest_inputs, tmp_path, monkeypatch):
    """The image has no weights: generation must not hash, list or open anything for a model."""
    declaration, lock, model_source, _expected = cpu_manifest_inputs
    hashed = []
    monkeypatch.setattr(runtime_verify, "_sha256", lambda path: hashed.append(path.name) or "0" * 64)
    real_verify = runtime_verify.verify
    verified = []
    monkeypatch.setattr(runtime_verify, "verify", lambda python, model_dir, manifest: (
        verified.append(model_dir), real_verify(python, model_dir, manifest))[1])
    runtime_verify.generate_manifest(Path(sys.executable), tmp_path / "qualified.json",
        runtime_lock=declaration, requirements_lock=lock, model_source_manifest=model_source)
    # Only the three source files are hashed for provenance; the runtime-only verify is given no directory.
    assert sorted(hashed) == sorted([declaration.name, lock.name, model_source.name])
    assert verified == [None]


@pytest.mark.parametrize("failure", ["version", "aggregate", "no_models", "verify"])
def test_cpu_generation_refuses_mismatch_and_leaves_no_manifest(cpu_manifest_inputs, tmp_path, monkeypatch, failure):
    declaration, lock, model_source, expected = cpu_manifest_inputs
    if failure == "version":
        monkeypatch.setattr(runtime_verify, "_installed_closure", lambda python, names: {
            "versions": {**expected, "easyocr": "unexpected"}, "python": "3.13.12"})
    elif failure == "aggregate":
        # A source authority whose aggregate is not the hash of its own per-file hashes is corrupt.
        source = json.loads(model_source.read_text())
        source["model_files"]["aggregate_sha256"] = "f" * 64
        model_source.write_text(json.dumps(source))
    elif failure == "no_models":
        model_source.write_text(json.dumps({"model_files": {"aggregate_sha256": "f" * 64}}))
    else:
        monkeypatch.setattr(runtime_verify, "verify", lambda *args: (_ for _ in ()).throw(RuntimeError("rejected")))
    output = tmp_path / "qualified.json"
    with pytest.raises(RuntimeError):
        runtime_verify.generate_manifest(Path(sys.executable), output,
            runtime_lock=declaration, requirements_lock=lock, model_source_manifest=model_source)
    assert not output.exists()
    assert not list(tmp_path.glob(".cognita-ocr-manifest-*"))


def test_runtime_only_verify_reads_no_model_directory(tmp_path, monkeypatch):
    runtime = {name: "qualified-test" for name in
               ("easyocr", "torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil")}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"runtime": runtime, "model_files": {"aggregate_sha256": "0" * 64}}), encoding="utf-8")
    monkeypatch.setattr(runtime_verify, "_probe", lambda _python: runtime)
    verify(Path(sys.executable), None, manifest)
    # With a directory the same manifest is refused: the aggregate of an empty directory is not the one listed.
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="aggregate SHA-256"):
        verify(Path(sys.executable), empty, manifest)
