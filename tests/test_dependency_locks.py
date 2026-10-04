"""Focused fail-closed dependency lock policy checks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("dependency_locks", ROOT / "scripts" / "dependency_locks.py")
assert SPEC and SPEC.loader
dependency_locks = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dependency_locks
SPEC.loader.exec_module(dependency_locks)


def test_hash_locked_parser_requires_exact_pin_and_hash() -> None:
    text = "# comment\nDemo_Package==1.2.3 --hash=sha256:" + "a" * 64 + "\n"
    result = dependency_locks.parse_hash_locked_requirements(text, role="service")
    assert result[0].name == "demo-package"
    assert result[0].version == "1.2.3"


@pytest.mark.parametrize(
    "text, message",
    [
        ("demo>=1\n", "exact version pin"),
        ("demo==1.0\n", "no SHA-256"),
        ("demo==1.0 --hash=sha256:" + "a" * 63 + "\n", "no SHA-256"),
        ("-r other.lock\n", "lock options"),
    ],
)
def test_hash_locked_parser_rejects_unqualified_inputs(text: str, message: str) -> None:
    with pytest.raises(dependency_locks.DependencyLockError, match=message):
        dependency_locks.parse_hash_locked_requirements(text, role="service")


def test_complete_lock_set_missing_from_current_source_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(dependency_locks.DependencyLockError, match="missing or linked"):
        dependency_locks.load_dependency_locks(tmp_path)


def test_embedding_exception_is_narrow_and_forbids_cpu_onnxruntime() -> None:
    digest = "a" * 64
    text = "fastembed==0.8.0 --hash=sha256:" + digest + "\n"
    text += "onnxruntime-migraphx==1.23.1 --hash=sha256:" + digest + "\n"
    records = dependency_locks.parse_hash_locked_requirements(text, role="embedding")
    dependency_locks.validate_lock_role(records, role="embedding")
    cpu_text = text + "onnxruntime==1.30.0 --hash=sha256:" + digest + "\n"
    cpu_records = dependency_locks.parse_hash_locked_requirements(cpu_text, role="embedding")
    with pytest.raises(dependency_locks.DependencyLockError, match="CPU onnxruntime"):
        dependency_locks.validate_lock_role(cpu_records, role="embedding")


def test_runtime_lock_validates_existing_committed_artifact_bindings() -> None:
    lock = ROOT / "containers/cognita-amd/runtime-lock.json"
    result = dependency_locks.validate_runtime_lock(lock, repo_root=ROOT)
    assert result["embedding"]["provider"] == "MIGraphXExecutionProvider"


def test_runtime_lock_model_bindings_are_checked_against_the_qualification_manifest(tmp_path) -> None:
    """14.2.0: the weights are no longer committed, so a binding is a name and a hash, and the hash must be
    the one the qualification manifest lists (the one authority)."""
    import json
    import shutil
    root = tmp_path / "repo"
    for rel in ("containers/cognita-amd/runtime-lock.json", "containers/cognita-amd/Dockerfile",
                "scripts/amd_runtime_preflight.py", "docs/easyocr-qualification-dependencies.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, root / rel)
    lock_path = root / "containers/cognita-amd/runtime-lock.json"
    dependency_locks.validate_runtime_lock(lock_path, repo_root=root)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["artifacts"]["ocr_models"]["english_g2.pth"]["sha256"] = "a" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(dependency_locks.DependencyLockError, match="OCR model binding mismatch: english_g2.pth"):
        dependency_locks.validate_runtime_lock(lock_path, repo_root=root)


def _records(*names: str) -> tuple:
    digest = "a" * 64
    text = "".join(f"{name}==1.0 --hash=sha256:{digest}\n" for name in names)
    return dependency_locks.parse_hash_locked_requirements(text, role="test")


def test_nvidia_roles_are_lock_roles_with_their_own_files() -> None:
    """15.0.0 (DESIGN-NVIDIA-ACCELERATION 5.6): both roles are in LOCK_ROLES and map to their lock files."""
    assert {"embedding-nvidia", "ocr-nvidia"} <= set(dependency_locks.LOCK_ROLES)
    assert dependency_locks.LOCK_FILES["embedding-nvidia"] == "embedding-nvidia-requirements.lock"
    assert dependency_locks.LOCK_FILES["ocr-nvidia"] == "ocr-nvidia-requirements.lock"


def test_embedding_nvidia_role_requires_gpu_onnxruntime_and_forbids_the_other_providers() -> None:
    dependency_locks.validate_lock_role(_records("fastembed", "onnxruntime-gpu"), role="embedding-nvidia")
    with pytest.raises(dependency_locks.DependencyLockError, match="requires both"):
        dependency_locks.validate_lock_role(_records("fastembed"), role="embedding-nvidia")
    with pytest.raises(dependency_locks.DependencyLockError, match="CPU onnxruntime"):
        dependency_locks.validate_lock_role(_records("fastembed", "onnxruntime-gpu", "onnxruntime"), role="embedding-nvidia")
    with pytest.raises(dependency_locks.DependencyLockError, match="onnxruntime-migraphx"):
        dependency_locks.validate_lock_role(_records("fastembed", "onnxruntime-gpu", "onnxruntime-migraphx"), role="embedding-nvidia")
    # The AMD rule is unchanged: it still demands MIGraphX and forbids the GPU wheel's CPU twin.
    with pytest.raises(dependency_locks.DependencyLockError, match="FastEmbed/MIGraphX"):
        dependency_locks.validate_lock_role(_records("fastembed", "onnxruntime-gpu"), role="embedding")


def test_ocr_nvidia_role_requires_the_runtime_and_forbids_triton() -> None:
    dependency_locks.validate_lock_role(_records("easyocr", "torch", "torchvision", "nvidia-cudnn-cu13"), role="ocr-nvidia")
    with pytest.raises(dependency_locks.DependencyLockError, match="incomplete"):
        dependency_locks.validate_lock_role(_records("easyocr", "torch"), role="ocr-nvidia")
    for forbidden in ("triton", "triton-rocm", "rocm-sdk", "onnxruntime"):
        with pytest.raises(dependency_locks.DependencyLockError, match="forbidden"):
            dependency_locks.validate_lock_role(_records("easyocr", "torch", "torchvision", forbidden), role="ocr-nvidia")


def test_nvidia_runtime_lock_is_validated_with_its_artifact_bindings() -> None:
    lock = ROOT / "containers/cognita-nvidia/runtime-lock.json"
    result = dependency_locks.validate_runtime_lock(lock, repo_root=ROOT)
    assert result["embedding"]["provider"] == "CUDAExecutionProvider"
    assert result["embedding"]["cuda_major"] == 13 and result["ocr"]["triton"] == "excluded"


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda v: v["embedding"].update(cuda_major=12), "not CUDA 13"),
        (lambda v: v["embedding"].update(provider="MIGraphXExecutionProvider"), "not CUDA"),
        (lambda v: v["embedding"].update(provider_wheel={"origin": "https://x.invalid/"}), "PyPI-only"),
        (lambda v: v["embedding"].update(python_abi="cp312"), "cp313"),
        (lambda v: v["embedding"].update(nvidia_wheels={}), "no NVIDIA wheels"),
        (lambda v: v["embedding"].update(nvidia_wheels={"cuda-python": "1"}), "malformed"),
        (lambda v: v["ocr"].update(triton="3.8.0"), "exclude triton"),
        (lambda v: v["ocr"].update(vendor_wheels={}), "exclude triton"),
        (lambda v: v["ocr"].update(cuda_major=12), "not CUDA 13"),
        (lambda v: v["driver_floor"].update(linux="R580"), "driver floor"),
        (lambda v: v["ocr"].pop("torch"), "missing field: torch"),
    ],
)
def test_nvidia_runtime_lock_structure_fails_closed(tmp_path: Path, edit, message: str) -> None:
    import json
    value = json.loads((ROOT / "containers/cognita-nvidia/runtime-lock.json").read_text(encoding="utf-8"))
    edit(value)
    path = tmp_path / "runtime-lock.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(dependency_locks.DependencyLockError, match=message):
        dependency_locks.validate_runtime_lock(path)


def test_nvidia_runtime_lock_artifact_hash_mismatch_is_refused(tmp_path: Path) -> None:
    import json
    import shutil
    for rel in ("containers/cognita-nvidia/runtime-lock.json", "containers/cognita-nvidia/Dockerfile",
                "scripts/nvidia_runtime_preflight.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, tmp_path / rel)
    path = tmp_path / "containers/cognita-nvidia/runtime-lock.json"
    dependency_locks.validate_runtime_lock(path, repo_root=tmp_path)
    original = (tmp_path / "scripts/nvidia_runtime_preflight.py").read_bytes()
    (tmp_path / "scripts/nvidia_runtime_preflight.py").write_bytes(b"# tampered\n")
    with pytest.raises(dependency_locks.DependencyLockError, match="hash mismatch: scripts/nvidia_runtime_preflight.py"):
        dependency_locks.validate_runtime_lock(path, repo_root=tmp_path)
    (tmp_path / "scripts/nvidia_runtime_preflight.py").write_bytes(original)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["artifacts"]["dockerfile_source"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(dependency_locks.DependencyLockError, match="missing artifact binding: dockerfile_source"):
        dependency_locks.validate_runtime_lock(path, repo_root=tmp_path)


def test_nvidia_ocr_lock_follows_the_runtime_lock_and_refuses_direct_urls(tmp_path: Path) -> None:
    import json
    declaration = ROOT / "containers/cognita-nvidia/runtime-lock.json"
    text = (ROOT / "containers/ocr-nvidia-requirements.lock").read_text(encoding="utf-8")
    versions = dependency_locks.validate_nvidia_ocr_lock(declaration, ROOT / "containers/ocr-nvidia-requirements.lock")
    ocr = json.loads(declaration.read_text(encoding="utf-8"))["ocr"]
    assert versions["torch"] == ocr["torch"] and "+" not in versions["torch"] and "triton" not in versions
    stale = tmp_path / "stale.lock"
    stale.write_text(text.replace(f"torch=={ocr['torch']}", "torch==2.13.0", 1), encoding="utf-8")
    with pytest.raises(dependency_locks.DependencyLockError, match="version mismatch: torch"):
        dependency_locks.validate_nvidia_ocr_lock(declaration, stale)
    digest = "a" * 64
    direct = tmp_path / "direct.lock"
    direct.write_text(text.rstrip("\n") + f"\nextra @ https://example.invalid/extra.whl#sha256={digest} --hash=sha256:{digest}\n", encoding="utf-8")
    with pytest.raises(dependency_locks.DependencyLockError, match="PyPI only"):
        dependency_locks.validate_nvidia_ocr_lock(declaration, direct)
    # The CPU declaration is not an NVIDIA lock and must not be accepted as one.
    with pytest.raises(dependency_locks.DependencyLockError):
        dependency_locks.validate_nvidia_ocr_lock(ROOT / "containers/cognita/runtime-lock.json", ROOT / "containers/ocr-nvidia-requirements.lock")


def test_install_policy_rejects_unlocked_resolution() -> None:
    with pytest.raises(dependency_locks.DependencyLockError, match="verified lock"):
        dependency_locks.assert_locked_install(("python", "-m", "pip", "install", "demo>=1"), role="service")
    dependency_locks.assert_locked_install(("python", "-m", "pip", "install", "--no-index", "demo.whl"), role="service")
