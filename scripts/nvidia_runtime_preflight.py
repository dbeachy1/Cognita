"""Bounded, non-publishing NVIDIA image qualification (DESIGN-NVIDIA-ACCELERATION 5.7).

This runs only inside the NVIDIA image, on a host with a card. It deliberately performs the same live
provider/session work as Admin verification, but exits with a small category rather than exposing worker
paths or native error text. It is the twin of ``amd_runtime_preflight.py`` (which is not touched) and
shares no code with it.

What the image build can prove without a card is static (the provider is registered, ``ldd`` resolves
everything but the two driver sonames). The live canaries below can only run where a card is, so the
qualification of a published NVIDIA image happens through Admin verification on real hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import struct
import subprocess
import sys
import sysconfig
import time
import zlib
from pathlib import Path
from types import SimpleNamespace


LOCK = Path("/opt/cognita-runtimes/runtime-lock.json")
MODEL_CACHE = Path("/var/lib/cognita/models")
MODEL_NAME = "BAAI/bge-large-en-v1.5"
CANARY = "Cognita indexes documents and answers questions about them."
# The marker file the embed venv's build leaves at its root (see the Dockerfile): the
# colon-joined site-packages/nvidia/*/lib directories the CUDA provider needs on LD_LIBRARY_PATH.
LIBRARY_PATH_MARKER = ".ld_library_path"


def _log(message: str) -> None:
    """One stderr line per decision. Bounded tokens only: never a native error text or a path."""
    print(message, file=sys.stderr, flush=True)


def _png() -> bytes:
    raw = b"\x00\xff\xff\xff\xff"
    result = b"\x89PNG\r\n\x1a\n"
    result += _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    result += _chunk(b"IDAT", zlib.compress(raw, 9))
    return result + _chunk(b"IEND", b"")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _service_packages_on_path() -> None:
    """Make the service's ``cognita`` package importable from a worker venv.

    The script runs under the embed or OCR venv (``_embed``/``_ocr`` assert it), and neither venv has
    ``cognita`` installed: the service does, in the interpreter both venvs were made from
    (``python -m venv`` in the Dockerfile), which is ``sys.base_prefix``. 15.0.0 imported
    ``cognita.gpu_probe`` without this and every run stopped at ``runtime_integrity_failed`` before
    reaching a card (found on Maia's 4090, 2026-09-30). APPENDED, so the venv's own packages
    (onnxruntime-gpu, torch) still win over the service's CPU builds of the same names."""
    purelib = sysconfig.get_paths(vars={"base": sys.base_prefix, "platbase": sys.base_prefix})["purelib"]
    if purelib not in sys.path:
        sys.path.append(purelib)
        _log("NVIDIA_PREFLIGHT_SERVICE_PACKAGES appended")


def _cards() -> list:
    _service_packages_on_path()
    from cognita.gpu_probe import default_probe

    return default_probe().devices()


def _uuid_bindable_cards(cards: list, component: str) -> list:
    """Return cards this isolated canary can bind without guessing an ordinal.

    The canary scopes a fresh process with ``CUDA_VISIBLE_DEVICES=GPU-<uuid>``. NVML always reports a
    UUID, but a card without one is still never certified by ordinal, the same rule the AMD script
    keeps. The missing identity stays visible in the bounded evidence while other cards can prove the
    image. The Admin verifier reports a selected UUID-less OCR card as ``stable_uuid_missing``."""
    bindable = [card for card in cards if card.unique_id]
    skipped = len(cards) - len(bindable)
    _log(f"NVIDIA_{component.upper()}_CARDS discovered={len(cards)} bindable={len(bindable)}")
    if skipped:
        _log(f"NVIDIA_{component.upper()}_SKIPPED stable_uuid_missing={skipped}")
    if not bindable:
        raise RuntimeError("stable_uuid_missing")
    return bindable


def _require_bound_pci(card) -> None:
    """Refuse to certify a session whose CUDA ordinal 0 resolved to another card."""
    _service_packages_on_path()
    from cognita.gpu_worker import bound_pci_address

    if bound_pci_address() != card.pci_address.lower():
        raise RuntimeError("canary_failed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(path: Path) -> str:
    """Hash canonical LF bytes for text copied into the image build context."""
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _load_lock(*, models: bool = True) -> dict:
    """Load and check the runtime lock. ``models=False`` skips the OCR weight files.

    The weights live in the model cache (``runtime_paths.ocr_models``), not in the image, so only the
    OCR component may require them; the embedding canary must not fail because the OCR weights have
    not been downloaded yet. The qualification manifest is generated at image build from this very
    lock, so its provenance hash must equal this file's."""
    try:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        if not (Path("/dev/nvidiactl").exists() or Path("/dev/dxg").exists()):
            raise RuntimeError("device_nodes_missing")
        assert lock["embedding"]["provider"] == "CUDAExecutionProvider"
        assert lock["embedding"]["cuda_major"] == 13 and lock["ocr"]["cuda_major"] == 13
        assert lock["runtime_paths"]["ocr_python"]
        manifest_path = Path(lock["ocr"]["model_manifest"])
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["provenance"]["runtime_lock_sha256"] == _sha256(LOCK)
        if models:
            model_dir = Path(lock["runtime_paths"]["ocr_models"])
            expected = {name: digest for name, digest in manifest["model_files"].items() if name.endswith(".pth")}
            assert {path.name for path in model_dir.glob("*.pth")} == set(expected)
            for name, digest in expected.items():
                assert _sha256(model_dir / name) == digest
        assert _sha256_text(Path(__file__)) == lock["artifacts"]["preflight_source"]["sha256"]
        return lock
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - preflight must expose only a category
        raise RuntimeError("runtime_integrity_failed") from exc


_CATEGORIES = {
    "runtime_integrity_failed", "device_nodes_missing", "stable_uuid_missing",
    "provider_cpu_fallback", "canary_failed", "verification_timeout", "driver_too_old",
}
_CHILD_FAILURE_CODES = {
    category: 20 + index for index, category in enumerate(sorted(_CATEGORIES))
}
_CHILD_CODE_CATEGORIES = {code: category for category, code in _CHILD_FAILURE_CODES.items()}


def _ensure_library_path() -> None:
    """Put the embed venv's CUDA library directories on LD_LIBRARY_PATH, re-executing once if needed.

    Without them ``CUDAExecutionProvider`` is still "available" but the live session runs on the CPU
    (measured in the design's spike), which is exactly what the provider_cpu_fallback check would then
    report. The service does the same for its worker (``gpu_host._worker_library_path``); this script
    is started by hand, so it must do it for itself. The marker is read unresolved, beside the venv."""
    marker = Path(sys.executable).parent.parent / LIBRARY_PATH_MARKER
    if not marker.is_file():
        _log("NVIDIA_PREFLIGHT_LIBRARY_PATH marker=absent")
        return
    wanted = marker.read_text(encoding="utf-8").strip()
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if not wanted or current == wanted or current.startswith(wanted + ":"):
        return
    env = dict(os.environ, LD_LIBRARY_PATH=f"{wanted}:{current}" if current else wanted)
    _log(f"NVIDIA_PREFLIGHT_LIBRARY_PATH re-exec dirs={wanted.count(':') + 1}")
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


def _run_isolated_embedding_card(card, deadline: float) -> None:
    """Run one card's CUDA canary in a fresh process scoped to that card.

    CUDA keeps the first visible device in process-global state, so a second UUID cannot be proven in
    the same process. The parent owns each child and preserves the stable category contract when it
    rejects its card."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("verification_timeout")
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--component", "embedding", "--timeout", str(min(120.0, remaining)),
        "--card-uuid", str(card.unique_id),
    ]
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = f"GPU-{card.unique_id}"
    try:
        # Provider libraries may write native diagnostics to stdout/stderr. Only the child's stable
        # exit category crosses this boundary.
        completed = subprocess.run(
            command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=remaining, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("verification_timeout") from exc
    if completed.returncode:
        raise RuntimeError(_CHILD_CODE_CATEGORIES.get(completed.returncode, "canary_failed"))


def _embed(deadline: float, card_uuid: str | None = None) -> None:
    import onnxruntime as ort
    from fastembed import TextEmbedding

    lock = _load_lock(models=False)
    expected = lock["embedding"]
    assert lock["runtime_paths"]["embedding_python"] == sys.executable
    assert sys.version_info[:2] == (3, 13)
    assert expected["python_abi"] == "cp313"
    for package in ("fastembed", "onnxruntime-gpu"):
        assert importlib.metadata.version(package) == expected[package]
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("runtime_integrity_failed")
    discovered = _cards()
    if not discovered:
        raise RuntimeError("device_nodes_missing")
    cards = _uuid_bindable_cards(discovered, "embedding")
    if card_uuid is None and len(cards) > 1:
        for card in cards:
            _run_isolated_embedding_card(card, deadline)
            print(f"NVIDIA_EMBEDDING_CARD_CANARY_OK GPU-{card.unique_id}")
        print("NVIDIA_EMBEDDING_SESSION_VECTOR_CANARY_OK")
        return
    if card_uuid is not None:
        cards = [card for card in cards if card.unique_id == card_uuid]
        if not cards:
            raise RuntimeError("stable_uuid_missing")
    cpu = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE), providers=["CPUExecutionProvider"])
    reference = next(cpu.embed([CANARY])).tolist()
    for card in cards:
        if time.monotonic() >= deadline:
            raise TimeoutError("verification_timeout")
        if not card.unique_id:
            raise RuntimeError("stable_uuid_missing")
        # A child started for one card already has this from its parent; a lone card in the parent
        # process needs it set before the first session creates the CUDA context.
        os.environ["CUDA_VISIBLE_DEVICES"] = f"GPU-{card.unique_id}"
        gpu = None
        try:
            try:
                gpu = TextEmbedding(
                    model_name=MODEL_NAME,
                    cache_dir=str(MODEL_CACHE),
                    providers=[("CUDAExecutionProvider", {"device_id": 0})],
                )
            except Exception as exc:  # noqa: BLE001 - only a category leaves this script
                # ORT's own text for a driver older than the CUDA 13 floor (R580).
                if "driver version is insufficient" in str(exc).lower():
                    raise RuntimeError("driver_too_old") from exc
                raise RuntimeError("canary_failed") from exc
            session = getattr(getattr(gpu, "model", None), "model", None)
            active = list(session.get_providers()) if session is not None else []
            if "CUDAExecutionProvider" not in active:
                raise RuntimeError("provider_cpu_fallback")
            candidate = next(gpu.embed([CANARY])).tolist()
            _require_bound_pci(card)
            if len(candidate) != len(reference) or not all(math.isfinite(float(v)) for v in candidate):
                raise RuntimeError("canary_failed")
            delta = max(abs(float(a) - float(b)) for a, b in zip(reference, candidate, strict=True))
            _log(f"NVIDIA_EMBEDDING_CANARY_DELTA card=GPU-{card.unique_id} delta={delta:.2e}")
            if delta > 1e-4:
                raise RuntimeError("canary_failed")
        finally:
            del gpu
            gc.collect()
    print("NVIDIA_EMBEDDING_SESSION_VECTOR_CANARY_OK")


def _ocr(deadline: float) -> None:
    _service_packages_on_path()
    from cognita.assets.ocr_worker import OCRWorkerRunner

    lock = _load_lock()
    assert lock["runtime_paths"]["ocr_python"] == sys.executable
    for package in ("easyocr", "torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil"):
        expected = lock["ocr"][package]
        installed = importlib.metadata.version(package)
        assert installed == expected, package
    discovered = _cards()
    if not discovered:
        raise RuntimeError("device_nodes_missing")
    cards = _uuid_bindable_cards(discovered, "ocr")
    config = SimpleNamespace(
        ocr_timeout_s=max(1.0, deadline - time.monotonic()),
        ocr_max_pixels=4_000_000,
        ocr_max_dimension=4096,
        ocr_cpu_threads=1,
        ocr_qualification_manifest=lock["ocr"]["model_manifest"],
    )
    for card in cards:
        if time.monotonic() >= deadline:
            raise TimeoutError("verification_timeout")
        if not card.unique_id:
            raise RuntimeError("stable_uuid_missing")
        runner = OCRWorkerRunner(
            config,
            python=sys.executable,
            model_dir=lock["runtime_paths"]["ocr_models"],
            device="gpu",
            device_id=f"GPU-{card.unique_id}",
        )
        payload = asyncio.run(runner.run(_png(), ("en",), timeout_seconds=max(0.1, deadline - time.monotonic()), device="gpu", device_id=f"GPU-{card.unique_id}"))
        _log(f"NVIDIA_OCR_CARD card=GPU-{card.unique_id} device={payload.device} backend={payload.backend}")
        if payload.device != "gpu" or payload.device_binding != f"GPU-{card.unique_id}" or payload.backend != "pytorch-cuda":
            raise RuntimeError("canary_failed")
    print("NVIDIA_OCR_SESSION_QUALIFICATION_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=("embedding", "ocr"), required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--card-uuid")
    args = parser.parse_args()
    try:
        deadline = time.monotonic() + max(1.0, min(args.timeout, 120.0))
        if args.component == "embedding":
            _ensure_library_path()
            _embed(deadline, args.card_uuid)
        else:
            if args.card_uuid is not None:
                raise RuntimeError("stable_uuid_missing")
            _ocr(deadline)
        return 0
    except TimeoutError:
        _log("NVIDIA_PREFLIGHT_FAILED verification_timeout")
        return _CHILD_FAILURE_CODES["verification_timeout"] if args.card_uuid is not None else 1
    except Exception as exc:  # noqa: BLE001 - stable category is the contract
        category = str(exc) if str(exc) in _CATEGORIES else "runtime_integrity_failed"
        _log(f"NVIDIA_PREFLIGHT_FAILED {category}")
        return _CHILD_FAILURE_CODES[category] if args.card_uuid is not None else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
