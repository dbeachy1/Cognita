#!/usr/bin/env python3
"""Verify a pre-provisioned OCR interpreter and model tree without network access."""

from __future__ import annotations

import argparse
import asyncio
import signal
import time
import tempfile
import hashlib
import json
import os
import subprocess
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(python: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, easyocr, torch; "
        "print('|'.join((easyocr.__version__, torch.__version__, "
        "m.version('torchvision'), m.version('Pillow'), "
        "m.version('opencv-python-headless'), m.version('numpy'), m.version('psutil'))))"
    )
    # Compose's numeric UID need not have a passwd entry. Torch's implicit
    # Inductor cache consults getpass.getuser(), even during an import. Own all
    # writable import caches explicitly; never add a username or relax UID.
    with tempfile.TemporaryDirectory(prefix="cognita-ocr-probe-") as cache:
        env = {key: os.environ[key] for key in ("PATH", "LD_LIBRARY_PATH", "ROCM_PATH", "HIP_PATH") if key in os.environ}
        env.update({"NO_PROXY": "*", "no_proxy": "*", "HOME": cache, "TMPDIR": cache,
                    "XDG_CACHE_HOME": str(Path(cache) / "cache"),
                    "TORCH_HOME": str(Path(cache) / "torch"),
                    "TORCHINDUCTOR_CACHE_DIR": str(Path(cache) / "torchinductor"),
                    "EASYOCR_MODULE_PATH": str(Path(cache) / "easyocr")})
        try:
            output = subprocess.run([str(python), "-I", "-B", "-c", code],
                                    check=True, capture_output=True, text=True, timeout=30, env=env)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip().replace("\n", " ")
            raise RuntimeError(f"OCR interpreter version probe failed: {detail[-1000:]}") from exc
    if Path(cache).exists():
        raise RuntimeError("OCR interpreter probe cache cleanup failed")
    values = output.stdout.strip().split("|")
    if len(values) != 7:
        raise RuntimeError("OCR interpreter returned an invalid version probe")
    return dict(zip(("easyocr", "torch", "torchvision", "Pillow",
                     "opencv-python-headless", "numpy", "psutil"), values, strict=True))


def verify(python: Path, model_dir: Path | None, manifest_path: Path) -> None:
    """Check the interpreter's package versions and, when ``model_dir`` is given, the model bytes.

    ``model_dir=None`` checks the runtime only. 14.2.0 (DESIGN-LINUX-INSTALLER 6.5): the images no
    longer carry the EasyOCR weights (they are downloaded into the model cache), so the image build
    verifies the runtime it just installed and reads no model file; the byte check happens at run
    time in ``ocr_worker._verify_qualification`` and in the release smoke against a fetched directory.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not python.is_file() or (model_dir is not None and not model_dir.is_dir()):
        raise RuntimeError("OCR interpreter or model directory is absent")
    actual_versions = _probe(python)
    expected_versions = {str(k): str(v) for k, v in manifest["runtime"].items()}
    engine_name, separator, engine_version = str(manifest.get("engine", "")).partition("==")
    if separator:
        if engine_name != "easyocr" or not engine_version:
            raise RuntimeError("OCR engine pin is invalid")
        expected_versions[engine_name] = engine_version
    if actual_versions != expected_versions:
        raise RuntimeError(
            f"OCR interpreter versions do not match qualification pins: "
            f"actual={actual_versions!r} expected={expected_versions!r}"
        )
    if model_dir is None:
        print("OCR_RUNTIME_VERIFIED (runtime packages only; no model bytes read)")
        return
    files = []
    for path in sorted(item for item in model_dir.rglob("*") if item.is_file()):
        files.append((str(path.relative_to(model_dir)), _sha256(path)))
    model_manifest = manifest["model_files"]
    aggregate = hashlib.sha256("".join(value for _, value in files).encode()).hexdigest()
    if aggregate != model_manifest["aggregate_sha256"]:
        raise RuntimeError("OCR model aggregate SHA-256 does not match qualification evidence")
    for name, expected in model_manifest.items():
        if name.endswith(".pth") and (model_dir / name).is_file() and _sha256(model_dir / name) == expected:
            continue
        if name.endswith(".pth"):
            raise RuntimeError(f"OCR model file checksum mismatch: {name}")
    print("OCR_RUNTIME_VERIFIED")


_RUNTIME_PACKAGES = ("torch", "torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil")


def _installed_closure(python: Path, names: list[str]) -> dict:
    code = ("import importlib.metadata as m,json,sys,platform; "
            "print(json.dumps({'versions':{n:m.version(n) for n in sys.argv[1:]},"
            "'python':platform.python_version(),'platform':platform.platform(),"
            "'libc':platform.libc_ver()}))")
    process = subprocess.Popen([str(python), "-I", "-B", "-c", code, *names],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env={"PATH": os.environ.get("PATH", ""), "NO_PROXY": "*"},
                               start_new_session=(os.name == "posix"))
    try:
        stdout, _stderr = process.communicate(timeout=60)
        if process.returncode:
            raise RuntimeError("OCR locked package probe failed")
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate(timeout=10)


def _declared_cuda_major(runtime_lock: Path) -> int | None:
    """The runtime-lock's `ocr.cuda_major` (the NVIDIA image's marker), or None for the CPU lock.

    A missing, unreadable or CPU-shaped file answers None so the CPU path runs unchanged and raises
    its own errors."""
    try:
        value = json.loads(runtime_lock.read_text(encoding="utf-8"))["ocr"]["cuda_major"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _nvidia_version_matches(name: str, imported: str, locked: str, suffix: str) -> bool:
    """The imported version equals the locked one, or (torch/torchvision only) the locked one plus the
    exact CUDA suffix the runtime-lock declares. Any other suffix, or a suffix on another package, is
    a mismatch; the base version must always be exactly the locked one."""
    if imported == locked:
        return True
    return name in ("torch", "torchvision") and imported == locked + suffix


def generate_manifest(python: Path, output: Path, *, runtime_lock: Path,
                      requirements_lock: Path, model_source_manifest: Path) -> dict:
    """Publish CPU or NVIDIA qualification facts only after matching the declared inputs.

    14.2.0 (DESIGN-LINUX-INSTALLER 6.5): the ``runtime`` section is generated from the installed CPU
    packages, but ``model_files`` and ``aggregate_sha256`` are taken from ``model_source_manifest``
    (``docs/easyocr-qualification-dependencies.json``) WITHOUT reading any model file, because the
    image no longer contains the weights. (Superseded: this function used to take the model directory,
    hash every file in it and refuse an image whose bytes differed from the authority.) The source
    manifest's own aggregate is still cross-checked against its per-file hashes, so a corrupt
    authority cannot be published. The CPU manifest is never the AMD one: that one pins the ROCm torch
    build, which would fail CPU OCR's runtime check."""
    try:
        from .dependency_locks import validate_cpu_ocr_lock, validate_nvidia_ocr_lock
    except ImportError:
        from dependency_locks import validate_cpu_ocr_lock, validate_nvidia_ocr_lock
    # 15.0.0 (DESIGN-NVIDIA-ACCELERATION 5.4): the NVIDIA image generates its manifest the same way,
    # from its own runtime-lock, which is recognised by declaring `ocr.cuda_major`. The CPU path below
    # is exactly what it was, including its error strings; only the NVIDIA branch differs (it names
    # NVIDIA in its errors and accepts one local-version suffix, see _nvidia_version_matches).
    cuda_major = _declared_cuda_major(runtime_lock)
    label = "NVIDIA" if cuda_major is not None else "CPU"
    if cuda_major is not None:
        expected = validate_nvidia_ocr_lock(runtime_lock, requirements_lock)
    else:
        expected = validate_cpu_ocr_lock(runtime_lock, requirements_lock)
    declaration = json.loads(runtime_lock.read_text(encoding="utf-8"))
    if Path(declaration["model_source_manifest"]).name != model_source_manifest.name:
        raise RuntimeError(f"{label} model source manifest differs from the declaration")
    probe = _installed_closure(python, list(expected))
    if probe["versions"] != expected or not probe["python"].startswith("3.13."):
        raise RuntimeError(f"OCR installed closure differs from the {label} source/lock")
    actual = _probe(python)
    qualified = {name: expected[name.lower()] for name in ("easyocr", *_RUNTIME_PACKAGES)}
    if cuda_major is not None:
        # PyPI's CUDA torch reports "2.14.0+cu130" as torch.__version__ while its distribution metadata
        # says "2.14.0". The manifest records the imported strings (what ocr_worker compares at run
        # time, as the AMD manifest already does for its "+rocm7.1" builds).
        suffix = f"+cu{cuda_major * 10}"
        mismatched = sorted(name for name in qualified
                            if not _nvidia_version_matches(name, actual.get(name, ""), qualified[name], suffix))
        print(f"ocr-manifest: profile=nvidia cuda_suffix={suffix} mismatched={mismatched or 'none'}", flush=True)
        if mismatched:
            raise RuntimeError(f"OCR imported versions differ from the {label} locked closure")
    elif actual != qualified:
        raise RuntimeError("OCR imported versions differ from the CPU locked closure")
    source_models = json.loads(model_source_manifest.read_text(encoding="utf-8"))["model_files"]
    expected_models = {name: digest for name, digest in sorted(source_models.items()) if name.endswith(".pth")}
    if not expected_models or any(not isinstance(digest, str) or len(digest) != 64 for digest in expected_models.values()):
        raise RuntimeError("OCR model source authority lists no valid model hashes")
    # The worker's aggregate is the hash of the per-file hashes in sorted-name order, so the authority's
    # aggregate must equal that of its own per-file hashes.
    aggregate = hashlib.sha256("".join(expected_models.values()).encode()).hexdigest()
    if aggregate != source_models.get("aggregate_sha256"):
        raise RuntimeError("OCR model source authority aggregate does not match its per-file hashes")
    print(f"ocr-manifest: model_files taken from {model_source_manifest.name} without reading model "
          f"bytes files={len(expected_models)} aggregate={aggregate}", flush=True)
    manifest = {
        "engine": "easyocr==" + expected["easyocr"],
        "runtime": {name: actual[name] for name in _RUNTIME_PACKAGES},
        "model_files": {**expected_models, "aggregate_sha256": aggregate},
        "provenance": {"runtime_lock_sha256": _sha256(runtime_lock),
                       "requirements_lock_sha256": _sha256(requirements_lock),
                       "model_source_manifest_sha256": _sha256(model_source_manifest),
                       "interpreter": {key: probe[key] for key in ("python", "platform", "libc")}},
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".cognita-ocr-manifest-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verify(python, None, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest


async def smoke(python: Path, model_dir: Path, manifest: Path) -> dict:
    """Exercise the actual child boundary using installed synthetic fixture bytes."""
    from importlib.resources import files
    from cognita.assets.ocr_worker import OCRWorkerRunner
    from cognita.assets.ocr_service import configured_pipeline_identity
    from cognita.config import CognitaConfig
    from cognita.selftest_fixtures import load_manifest
    verify(python, model_dir, manifest)
    config = CognitaConfig(ocr_python=str(python), ocr_model_dir=str(model_dir),
                           ocr_qualification_manifest=manifest, ocr_device="cpu")
    identity = configured_pipeline_identity(config, ("en",))
    if identity is None:
        raise RuntimeError("OCR configured pipeline rejects the packaged manifest")
    expected_engine = json.loads(manifest.read_text(encoding="utf-8"))["engine"].split("==", 1)[1]
    data = files("cognita.selftest_fixtures").joinpath("data")
    fixtures = {row.path: row for row in load_manifest()}
    runner = OCRWorkerRunner(config)
    started = time.monotonic()
    results = {}
    for name in ("canonical-clear.png", "blank.png"):
        spec = fixtures["cognita-selftest/ocr/" + name]
        image = data.joinpath(spec.path).read_bytes()
        if hashlib.sha256(image).hexdigest() != spec.sha256:
            raise RuntimeError("OCR installed fixture checksum mismatch: " + name)
        result = await runner.run(image, ("en",), timeout_seconds=config.ocr_timeout_s)
        if (result.device != "cpu" or result.backend != "pytorch-cpu" or result.engine_name != "easyocr"
                or result.engine_version != expected_engine or result.model_fingerprint != identity[0]):
            raise RuntimeError("OCR packaged CPU worker qualification mismatch: " + name)
        if bool(result.regions) != (name == "canonical-clear.png"):
            raise RuntimeError("OCR fixture recognition mismatch: " + name)
        if name == "canonical-clear.png" and not any(region.text.strip() for region in result.regions):
            raise RuntimeError("OCR canonical fixture returned empty text")
        results[name] = {"regions": len(result.regions), "device": result.device, "backend": result.backend}
    return {"status": "passed", "model_aggregate_sha256": json.loads(manifest.read_text(encoding="utf-8"))["model_files"]["aggregate_sha256"],
            "duration_seconds": round(time.monotonic() - started, 3),
            "fixtures": results, "model_fingerprint": identity[0], "worker_cleanup": "reaped"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--model-dir", type=Path,
                        help="the model directory to hash (verify and smoke); manifest generation reads no model bytes")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--generate-manifest", action="store_true")
    parser.add_argument("--runtime-lock", type=Path)
    parser.add_argument("--requirements-lock", type=Path)
    parser.add_argument("--model-source-manifest", type=Path)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.generate_manifest and (args.smoke or not all((args.runtime_lock, args.requirements_lock, args.model_source_manifest))):
        parser.error("manifest generation requires its three source inputs and excludes smoke")
    if args.generate_manifest and args.model_dir is not None:
        parser.error("manifest generation reads no model bytes; do not pass --model-dir")
    if not args.generate_manifest and args.model_dir is None:
        parser.error("--model-dir is required to verify or smoke-test a model directory")
    # Keep the venv launcher symlink intact.  Resolving it to uv's base
    # interpreter discards pyvenv.cfg and makes isolated imports miss the
    # dedicated environment's site-packages.
    if args.generate_manifest:
        generate_manifest(args.python.absolute(), args.manifest.absolute(),
                          runtime_lock=args.runtime_lock, requirements_lock=args.requirements_lock,
                          model_source_manifest=args.model_source_manifest)
    elif args.smoke:
        print(json.dumps(asyncio.run(smoke(args.python.absolute(), args.model_dir.resolve(), args.manifest.resolve())), sort_keys=True))
    else:
        verify(args.python.absolute(), args.model_dir.resolve(), args.manifest.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
