"""Bounded, non-publishing AMD image qualification.

This runs only inside the digest-pinned AMD image.  It deliberately performs
the same live provider/session work as Admin verification, but exits with a
small category rather than exposing worker paths or native error text.
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
import subprocess
import struct
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace


LOCK = Path("/opt/cognita-runtimes/runtime-lock.json")
MODEL_CACHE = Path("/var/lib/cognita/models")
PROGRAM_CACHE = MODEL_CACHE / "migraphx-cache"
MODEL_NAME = "BAAI/bge-large-en-v1.5"
CANARY = "Cognita indexes documents and answers questions about them."


def _png() -> bytes:
    raw = b"\x00\xff\xff\xff\xff"
    result = b"\x89PNG\r\n\x1a\n"
    result += _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    result += _chunk(b"IDAT", zlib.compress(raw, 9))
    return result + _chunk(b"IEND", b"")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _cards() -> list:
    from cognita.gpu_probe import default_probe

    return default_probe().devices()


def _uuid_bindable_cards(cards: list, component: str) -> list:
    """Return cards this isolated canary can bind without guessing an ordinal.

    The embedding service joins PCI discovery to its worker/provider identity and
    can therefore retain UUID-less cards for Knowledge fallback/selection.  This
    one-shot preflight, however, binds a direct runtime session with
    ``ROCR_VISIBLE_DEVICES``; using an ordinal for a card without a stable UUID
    could certify the wrong device.  OCR has the same constraint by contract:
    its worker visibility is UUID-based.  Keep the missing identity visible in
    the bounded evidence while allowing other qualified cards to prove the
    image.  The Admin verifier remains responsible for reporting a selected
    UUID-less OCR card as ``stable_uuid_missing`` and falling back to CPU.
    """
    bindable = [card for card in cards if card.unique_id]
    skipped = len(cards) - len(bindable)
    if skipped:
        print(
            f"AMD_{component.upper()}_SKIPPED stable_uuid_missing={skipped}",
            file=sys.stderr,
        )
    if not bindable:
        raise RuntimeError("stable_uuid_missing")
    return bindable


def _require_bound_pci(card) -> None:
    """Refuse to certify a session whose HIP ordinal resolved to another card."""
    from cognita.gpu_worker import bound_pci_address

    if bound_pci_address() != card.pci_address.lower():
        raise RuntimeError("canary_failed")


def _program_cache_dir() -> str:
    """Qualify with production's dedicated writable MIGraphX program cache."""
    if os.environ.get("COGNITA_GPU_PROGRAM_CACHE_DIR") != str(PROGRAM_CACHE):
        raise RuntimeError("runtime_integrity_failed")
    PROGRAM_CACHE.mkdir(parents=True, exist_ok=True)
    return str(PROGRAM_CACHE)


def _load_lock(*, models: bool = True) -> dict:
    """Load and check the runtime lock. ``models=False`` skips the OCR weight files.

    14.2.0 (DESIGN-LINUX-INSTALLER 6.5): the weights live in the model cache
    (``runtime_paths.ocr_models``), not in the image, so only the OCR component may require them;
    the embedding canary must not fail because the OCR weights have not been downloaded yet."""
    try:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        if not Path("/dev/kfd").is_char_device() or not Path("/dev/dri").is_dir():
            raise RuntimeError("device_nodes_missing")
        assert lock["embedding"]["provider"] == "MIGraphXExecutionProvider"
        assert lock["runtime_paths"]["ocr_python"]
        manifest = Path(lock["ocr"]["model_manifest"])
        assert manifest.is_file()
        expected_manifest = lock["artifacts"]["qualification_manifest"]["sha256"]
        assert _sha256_text(manifest) == expected_manifest
        if models:
            model_dir = Path(lock["runtime_paths"]["ocr_models"])
            assert {path.name for path in model_dir.glob("*.pth")} == {"craft_mlt_25k.pth", "english_g2.pth"}
            for name, item in lock["artifacts"]["ocr_models"].items():
                assert _sha256(model_dir / name) == item["sha256"]
        assert _sha256_text(Path(__file__)) == lock["artifacts"]["preflight_source"]["sha256"]
        return lock
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - preflight must expose only a category
        raise RuntimeError("runtime_integrity_failed") from exc


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


_CATEGORIES = {
    "runtime_integrity_failed", "device_nodes_missing", "stable_uuid_missing",
    "provider_cpu_fallback", "canary_failed", "verification_timeout",
}
_CHILD_FAILURE_CODES = {
    category: 20 + index for index, category in enumerate(sorted(_CATEGORIES))
}
_CHILD_CODE_CATEGORIES = {code: category for category, code in _CHILD_FAILURE_CODES.items()}


def _run_isolated_embedding_card(card, deadline: float) -> None:
    """Run one card's HIP/MIGraphX canary in a fresh process.

    HIP keeps the first visible device in process-global state.  Reusing the
    ORT session process for another UUID can therefore report a successful
    vector while still executing on the first card.  The parent owns each
    child and preserves the stable category contract when it rejects its card.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("verification_timeout")
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--component", "embedding", "--timeout", str(min(120.0, remaining)),
        "--card-uuid", str(card.unique_id),
    ]
    try:
        # Provider libraries may write native diagnostics to stdout/stderr.
        # Only the child's stable exit category crosses this boundary.
        completed = subprocess.run(
            command, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining, env=os.environ.copy(),
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
    assert sys.version_info[:2] == (3, 12)
    assert expected["python_abi"] == "cp312"
    for package in ("fastembed", "onnxruntime-migraphx"):
        assert importlib.metadata.version(package) == expected[package]
    assert "MIGraphXExecutionProvider" in ort.get_available_providers()
    discovered = _cards()
    if not discovered:
        raise RuntimeError("device_nodes_missing")
    cards = _uuid_bindable_cards(discovered, "embedding")
    if card_uuid is None and len(cards) > 1:
        for card in cards:
            _run_isolated_embedding_card(card, deadline)
            print(f"AMD_EMBEDDING_CARD_CANARY_OK GPU-{card.unique_id}")
        print("AMD_EMBEDDING_SESSION_VECTOR_CANARY_OK")
        return
    if card_uuid is not None:
        cards = [card for card in cards if card.unique_id == card_uuid]
        if not cards:
            raise RuntimeError("stable_uuid_missing")
    cpu = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE), providers=["CPUExecutionProvider"])
    reference = next(cpu.embed([CANARY])).tolist()
    # Keep qualification on the exact dedicated compiled-program directory
    # used by production. Passing the model root to MIGraphX made both free
    # discrete cards fail the 12.6 release canary (2026-09-19), while the
    # previous 12.5 canary without that override passed.
    program_cache_dir = _program_cache_dir()
    for card in cards:
        if time.monotonic() >= deadline:
            raise TimeoutError("verification_timeout")
        if not card.unique_id:
            raise RuntimeError("stable_uuid_missing")
        os.environ["ROCR_VISIBLE_DEVICES"] = f"GPU-{card.unique_id}"
        gpu = None
        try:
            # The Compose user can write the persisted model root, not the
            # image-owned /app working directory. Match the production worker's
            # explicit MIGraphX compiled-program cache during qualification.
            gpu = TextEmbedding(
                model_name=MODEL_NAME,
                cache_dir=str(MODEL_CACHE),
                providers=[("MIGraphXExecutionProvider", {
                    "device_id": 0,
                    "migraphx_model_cache_dir": program_cache_dir,
                })],
            )
            session = getattr(getattr(gpu, "model", None), "model", None)
            active = list(session.get_providers()) if session is not None else []
            if "MIGraphXExecutionProvider" not in active:
                raise RuntimeError("provider_cpu_fallback")
            candidate = next(gpu.embed([CANARY])).tolist()
            _require_bound_pci(card)
            if len(candidate) != len(reference) or not all(math.isfinite(float(v)) for v in candidate):
                raise RuntimeError("canary_failed")
            delta = max(abs(float(a) - float(b)) for a, b in zip(reference, candidate, strict=True))
            if delta > 1e-4:
                raise RuntimeError("canary_failed")
        finally:
            del gpu
            gc.collect()
    print("AMD_EMBEDDING_SESSION_VECTOR_CANARY_OK")


def _ocr(deadline: float) -> None:
    from cognita.assets.ocr_worker import OCRWorkerRunner

    lock = _load_lock()
    assert lock["runtime_paths"]["ocr_python"] == sys.executable
    for package in ("easyocr", "torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil"):
        expected = lock["ocr"][package]
        assert importlib.metadata.version(package) == expected
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
        if payload.device != "gpu" or payload.device_binding != f"GPU-{card.unique_id}":
            raise RuntimeError("canary_failed")
    print("AMD_OCR_SESSION_QUALIFICATION_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=("embedding", "ocr"), required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--card-uuid")
    args = parser.parse_args()
    try:
        deadline = time.monotonic() + max(1.0, min(args.timeout, 120.0))
        if args.component == "embedding":
            _embed(deadline, args.card_uuid)
        else:
            if args.card_uuid is not None:
                raise RuntimeError("stable_uuid_missing")
            _ocr(deadline)
        return 0
    except TimeoutError:
        print("AMD_PREFLIGHT_FAILED verification_timeout", file=sys.stderr)
        return _CHILD_FAILURE_CODES["verification_timeout"] if args.card_uuid is not None else 1
    except Exception as exc:  # noqa: BLE001 - stable category is the contract
        category = str(exc) if str(exc) in _CATEGORIES else "runtime_integrity_failed"
        print(f"AMD_PREFLIGHT_FAILED {category}", file=sys.stderr)
        return _CHILD_FAILURE_CODES[category] if args.card_uuid is not None else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
