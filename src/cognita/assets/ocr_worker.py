"""Bounded framed OCR worker and its parent-side runner.

The worker receives image bytes, never a source path.  A fresh process per
request keeps the process tree and native memory owned by one request and
makes timeout/cancellation cleanup auditable.  EasyOCR is deliberately loaded
only in the worker so MCP handlers and the host process remain dependency
light.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import socket
import struct
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .limits import MAX_DIMENSION
from .ocr_models import OCRRegion, OCRWorkerPayload

log = logging.getLogger("cognita.assets.ocr_worker")

_FRAME_LIMIT = 20 * 1024 * 1024
_HEADER = struct.Struct(">I")
_IMAGE_SOURCE_ROOT = Path("/app/src")


# 14.2.0 (DESIGN-LINUX-INSTALLER 6.5, D5): the EasyOCR weights are downloaded into the model cache
# instead of being baked into the image, so "the weights are not there" is now an ordinary state
# (the installer's download failed or has not run yet). This is the one message it gets, and the
# only detail an OCRWorkerError carries to its callers.
MISSING_WEIGHTS_MESSAGE = (
    "OCR model files are missing from the model cache. Rerun the installer to download them."
)


class OCRWorkerError(RuntimeError):
    """Safe reason from the isolated worker, without exposing stderr.

    ``message`` is empty except for the missing-weights case (``MISSING_WEIGHTS_MESSAGE``): the
    worker's other error messages are diagnostics and are not forwarded."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        self.message = message
        super().__init__(reason)


def _frame(payload: Mapping[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > _FRAME_LIMIT:
        raise ValueError("OCR worker frame exceeds its bound")
    return _HEADER.pack(len(body)) + body


async def _read_frame(stream: asyncio.StreamReader, *, limit: int = _FRAME_LIMIT) -> dict[str, Any]:
    header = await stream.readexactly(_HEADER.size)
    length = _HEADER.unpack(header)[0]
    if not 2 <= length <= limit:
        raise ValueError("OCR worker returned an invalid frame length")
    body = await stream.readexactly(length)
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("OCR worker returned a non-object frame")  # noqa: TRY004 - wire validation
    return value


def _safe_env(model_dir: str, device_id: str | None, *, cache_root: Path | None = None) -> dict[str, str]:
    # Keep only runtime/loader context needed by the pre-provisioned venv;
    # credentials, proxy settings, and unrelated application environment do
    # not cross the process boundary.
    env = {key: os.environ[key] for key in (
        "PATH", "LD_LIBRARY_PATH", "ROCM_PATH", "HIP_PATH",
        "HOME", "USER", "VIRTUAL_ENV", "SystemRoot",
        # These identify the caller's user systemd session.  They are routing
        # metadata rather than credentials and are required by systemd-run
        # when the child environment is explicitly sanitized below.
        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    ) if key in os.environ}
    # EasyOCR treats ``EASYOCR_MODULE_PATH`` as a writable module/cache root
    # and creates ``user_network`` beneath it during Reader initialization.
    # The qualified model tree is image-owned and immutable, so pointing this
    # variable at ``model_dir`` makes the sanitized child fail before OCR even
    # reaches the supplied models when Compose overrides the image UID.  Keep
    # the writable cache beside the service's HOME instead; ``model_dir``
    # remains the explicit, verified model_storage_directory below.
    easyocr_cache = Path(env.get("HOME", "/tmp")) / ".EasyOCR"
    env.update({
        "PYTHONNOUSERSITE": "1",
        "EASYOCR_MODULE_PATH": str(easyocr_cache),
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    if cache_root is not None:
        # Imports already initialize Torch's Inductor cache. An arbitrary
        # production UID may have no passwd entry, so deriving that cache from
        # a username fails before inference. This request owns every writable
        # cache until its complete child process tree has been reaped.
        env.pop("USER", None)
        env.update({"HOME": str(cache_root), "TMPDIR": str(cache_root),
                    "XDG_CACHE_HOME": str(cache_root / "cache"),
                    "EASYOCR_MODULE_PATH": str(cache_root / "easyocr"),
                    "TORCH_HOME": str(cache_root / "torch"),
                    "TORCHINDUCTOR_CACHE_DIR": str(cache_root / "torchinductor")})
    # ``-m cognita.assets.ocr_worker`` must resolve the image-owned source
    # package even when the dedicated interpreter has no application installed
    # into it.  In a packaged service, ``__file__`` points into the service
    # interpreter's site-packages; forwarding that parent makes the OCR venv
    # import the service installation and fail its qualified-runtime check.
    env["PYTHONPATH"] = str(_application_source_root())
    if device_id:
        # ROCm/Torch consults both HIP and ROCR visibility controls.  Set all
        # relevant selectors to the same physical UUID so a user-manager
        # environment containing a broader HIP_VISIBLE_DEVICES value cannot
        # silently widen or invalidate the requested binding.
        for key in ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
            env[key] = device_id
    return env


def _application_source_root(
    module_file: str | Path = __file__,
    *,
    packaged_root: str | Path = _IMAGE_SOURCE_ROOT,
) -> Path:
    """Choose the release source tree used by the isolated OCR interpreter.

    The AMD image retains the exact release source at ``/app/src`` after the
    service package is installed.  Prefer it when present so the OCR runtime
    never imports a package copied into the service interpreter's
    ``site-packages``.  Source checkouts and test installations fall back to
    the package-relative ``src`` root.
    """
    packaged = Path(packaged_root)
    local = Path(module_file).resolve().parents[2]
    for candidate in (packaged, local):
        if (candidate / "cognita" / "assets" / "ocr_worker.py").is_file():
            return candidate
    return local


class OCRWorkerRunner:
    """Run the offline EasyOCR worker with bounded input/output and cleanup."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        python: str | None = None,
        model_dir: str | Path | None = None,
        device: str = "cpu",
        device_id: str | None = None,
    ) -> None:
        self.config = config
        # An empty interpreter is deliberate: production OCR must be disabled
        # rather than silently importing EasyOCR from Cognita's service venv.
        self.python = str(python or getattr(config, "ocr_python", ""))
        self.launcher = tuple(getattr(config, "ocr_launcher", ()) or ())
        self.model_dir = str(model_dir or getattr(config, "ocr_model_dir", ""))
        self.device = device
        self.device_id = device_id
        self.qualification_manifest = str(getattr(config, "ocr_qualification_manifest", ""))

    async def run(self, image: bytes, languages: tuple[str, ...], *,
                  timeout_seconds: float | None = None, device: str | None = None,
                  device_id: str | None = None) -> OCRWorkerPayload:
        if len(image) > _FRAME_LIMIT:
            raise ValueError("OCR worker input exceeds its bound")
        if not self.python or not Path(self.python).is_file():
            raise OCRWorkerError("ocr_unavailable")
        if not self.model_dir or not Path(self.model_dir).is_dir():
            # The model cache mounts /var/lib/cognita/models, and easyocr/ inside it appears only once
            # the installer (or a deploy) has downloaded the weights.
            log.warning("ocr worker not started: model directory %r is absent; %s",
                        self.model_dir, MISSING_WEIGHTS_MESSAGE)
            raise OCRWorkerError("ocr_unavailable", MISSING_WEIGHTS_MESSAGE)
        if self.qualification_manifest and not Path(self.qualification_manifest).is_file():
            raise OCRWorkerError("ocr_unavailable")
        selected_device = device or self.device
        gpu = selected_device == "gpu" or selected_device.startswith("cuda")
        request = {
            "image": base64.b64encode(image).decode("ascii"),
            "languages": list(languages),
            "device": "cuda:0" if gpu else selected_device,
            "model_dir": self.model_dir,
            "max_pixels": int(getattr(self.config, "ocr_max_pixels", 16_777_216)),
            # Same clamp the service's pre-scan applies (13.0.2): the worker
            # must never accept an image the scan would have refused.
            "max_dimension": min(int(getattr(self.config, "ocr_max_dimension", 8_192)), MAX_DIMENSION),
            "max_regions": int(getattr(self.config, "ocr_max_regions", 10_000)),
            "cpu_threads": int(getattr(self.config, "ocr_cpu_threads", 2)),
            "memory_limit_mb": int(getattr(self.config, "ocr_worker_memory_mb", 2_048)),
            "qualification_manifest": self.qualification_manifest,
        }
        with tempfile.TemporaryDirectory(prefix="cognita-ocr-worker-") as cache:
            result = await self._run_framed(request, timeout_seconds=timeout_seconds,
                                          device_id=(device_id or self.device_id) if gpu else None,
                                          cache_root=Path(cache))
        if Path(cache).exists():
            raise OCRWorkerError("ocr_unavailable")
        return result

    async def _run_framed(self, request: dict, *, timeout_seconds: float | None,
                          device_id: str | None, cache_root: Path) -> OCRWorkerPayload:
        """Keep the cache alive through acquisition, execution and process teardown."""
        env = self._worker_env(device_id, cache_root=cache_root)
        command = [*self.launcher]
        # A user systemd manager does not inherit the caller's environment by
        # default.  Relay only the already-sanitized worker variables as
        # explicit argv values; never use a shell or forward credentials.
        if command and Path(command[0]).name == "systemd-run":
            command.extend(
                f"--setenv={key}={env[key]}"
                for key in (
                    "PYTHONPATH", "EASYOCR_MODULE_PATH", "PYTHONNOUSERSITE",
                    "NO_PROXY", "no_proxy", "ROCR_VISIBLE_DEVICES",
                    "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
                    "HOME", "TMPDIR", "XDG_CACHE_HOME", "TORCH_HOME", "TORCHINDUCTOR_CACHE_DIR",
                )
                if key in env
            )
        command.extend((
            self.python,
            "-m",
            "cognita.assets.ocr_worker",
            "--worker",
        ))
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        try:
            proc.stdin.write(_frame(request))
            await proc.stdin.drain()
            proc.stdin.close()
            # The parent owns the single end-to-end deadline.  The bounded
            # process cleanup intervals below are teardown, not a new OCR
            # execution budget.
            timeout = max(0.001, float(timeout_seconds if timeout_seconds is not None else getattr(self.config, "ocr_timeout_s", 60.0)))
            result = await asyncio.wait_for(_read_frame(proc.stdout), timeout=timeout)
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            if proc.returncode != 0:
                raise RuntimeError("OCR worker exited unsuccessfully")
            if result.get("status") != "success":
                reason = str(result.get("reason", "ocr_unavailable"))
                message = str(result.get("message", ""))
                raise OCRWorkerError(reason, message if message == MISSING_WEIGHTS_MESSAGE else "")
            return _payload_from_wire(result)
        except asyncio.CancelledError:
            await self._terminate(proc)
            raise
        except TimeoutError as exc:
            await self._terminate(proc)
            raise TimeoutError("OCR worker deadline exceeded") from exc
        finally:
            if proc.returncode is None:
                await self._terminate(proc)
            # Drain and await the exact owned child; stderr is intentionally
            # discarded rather than exposed to callers (it may contain paths).
            try:
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except TimeoutError:
                await self._terminate(proc)
            try:
                await asyncio.wait_for(stderr_task, timeout=1.0)
            except TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    def _worker_env(self, device_id: str | None = None, *, cache_root: Path | None = None) -> dict[str, str]:
        """Build the sanitized environment for one selected physical UUID."""
        return _safe_env(self.model_dir, device_id, cache_root=cache_root)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        if os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            await proc.wait()


async def _drain_stderr(stream: asyncio.StreamReader) -> None:
    """Drain diagnostics without retaining or exposing unbounded stderr."""
    while await stream.read(64 * 1024):
        pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_qualification(model_dir: str, manifest_path: str, easyocr: Any, torch: Any) -> bool:
    """Fail closed on a runtime/model tree that is not the qualified one."""
    if not manifest_path:
        return False
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        runtime = manifest["runtime"]
        engine_name, separator, engine_version = str(manifest["engine"]).partition("==")
        if not separator or engine_name != "easyocr" or str(getattr(easyocr, "__version__", "")) != engine_version:
            return False
        packages = {
            "easyocr": str(getattr(easyocr, "__version__", "")),
            "torch": str(getattr(torch, "__version__", "")),
        }
        import importlib.metadata
        for package in ("torchvision", "Pillow", "opencv-python-headless", "numpy", "psutil"):
            packages[package] = importlib.metadata.version(package)
        if any(packages.get(name) != str(version) for name, version in runtime.items()):
            return False
        root = Path(model_dir)
        files = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            files.append((str(path.relative_to(root)), _sha256_file(path)))
        aggregate = hashlib.sha256("".join(value for _, value in files).encode()).hexdigest()
        expected_models = manifest["model_files"]
        if aggregate != str(expected_models["aggregate_sha256"]):
            return False
        for name, expected in expected_models.items():
            if name.endswith(".pth") and (root / name).is_file() is False:
                return False
            if name.endswith(".pth") and _sha256_file(root / name) != str(expected):
                return False
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ImportError):
        return False
    return True


def _weights_missing(model_dir: str, manifest_path: str) -> bool:
    """True when the manifest names a ``.pth`` file that is not a file in ``model_dir``.

    Called only after ``_verify_qualification`` has already failed, to pick the message: a file that is
    present with the wrong bytes is NOT missing and keeps the generic message."""
    try:
        names = [name for name in json.loads(Path(manifest_path).read_text(encoding="utf-8"))["model_files"]
                 if name.endswith(".pth")]
    except (OSError, KeyError, TypeError, ValueError):
        return False
    root = Path(model_dir)
    return any(not (root / name).is_file() for name in names)


def _payload_from_wire(value: Mapping[str, Any]) -> OCRWorkerPayload:
    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list):
        raise ValueError("OCR worker regions are invalid")  # noqa: TRY004 - wire validation
    regions: list[OCRRegion] = []
    for item in raw_regions:
        if not isinstance(item, Mapping):
            raise ValueError("OCR worker region is invalid")  # noqa: TRY004 - wire validation
        confidence = item.get("confidence")
        confidence = None if confidence is None else float(confidence)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("OCR worker confidence is invalid")
        bbox = tuple(int(v) for v in item["bbox"])
        polygon = tuple(tuple(int(v) for v in point) for point in item["polygon"])
        if len(bbox) != 4 or not polygon:
            raise ValueError("OCR worker geometry is invalid")
        regions.append(OCRRegion(
            str(item["text"]), bbox, polygon, confidence,
            int(item["paragraph"]), int(item["line"]), int(item["order"]),
        ))
    model_fingerprint = str(value["model_fingerprint"])
    if len(model_fingerprint) != 64:
        raise ValueError("OCR worker model fingerprint is invalid")
    try:
        int(model_fingerprint, 16)
    except ValueError as exc:
        raise ValueError("OCR worker model fingerprint is invalid") from exc
    return OCRWorkerPayload(
        int(value["width"]), int(value["height"]), tuple(regions),
        str(value["device"]), str(value["backend"]), str(value["engine_name"]),
        str(value["engine_version"]), model_fingerprint,
        str(value.get("device_binding", "")),
        int(value.get("pipeline_version", 1)),
    )


def _decode_and_ocr(request: Mapping[str, Any]) -> dict[str, Any]:
    """Worker entry point; imports heavyweight optional dependencies lazily."""
    _disable_network()
    try:
        import easyocr
        import numpy as np
        import torch
        from PIL import Image, ImageFile
    except Exception as exc:  # noqa: BLE001 - dependency boundary
        return {"status": "error", "reason": "ocr_unavailable", "message": type(exc).__name__}

    try:
        if not _verify_qualification(str(request["model_dir"]), str(request.get("qualification_manifest", "")), easyocr, torch):
            # A wrong or missing file is still refused by its hash (unchanged); only the wording of the
            # missing-files case is specific, because that one has a remedy the operator can act on.
            if _weights_missing(str(request["model_dir"]), str(request.get("qualification_manifest", ""))):
                return {"status": "error", "reason": "ocr_unavailable", "message": MISSING_WEIGHTS_MESSAGE}
            return {"status": "error", "reason": "ocr_unavailable", "message": "qualified OCR runtime/model tree is unavailable"}
        raw = base64.b64decode(str(request["image"]), validate=True)
        max_pixels = int(request["max_pixels"])
        max_dimension = int(request["max_dimension"])
        Image.MAX_IMAGE_PIXELS = max_pixels
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        import io

        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG":
                return {"status": "error", "reason": "wrong_media_type", "message": "not PNG"}
            if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                return {"status": "error", "reason": "animated_png", "message": "animation unsupported"}
            if image.width > max_dimension or image.height > max_dimension:
                return {"status": "error", "reason": "too_large", "message": "dimensions exceed limit"}
            if image.width * image.height > max_pixels:
                return {"status": "error", "reason": "too_large", "message": "pixels exceed limit"}
            if image.width * image.height * 4 > max_pixels * 4:
                return {"status": "error", "reason": "too_large", "message": "decoded image exceeds limit"}
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            rgb = np.asarray(background.convert("RGB"))
            width, height = image.width, image.height
        threads = max(1, int(request["cpu_threads"]))
        torch.set_num_threads(threads)
        requested = str(request.get("device", "cpu"))
        if requested.startswith("cuda"):
            if not torch.cuda.is_available():
                return {"status": "error", "reason": "ocr_unavailable", "message": "GPU unavailable"}
            gpu: Any = requested
            device = "gpu"
            backend = "pytorch-rocm" if getattr(torch.version, "hip", None) else "pytorch-cuda"
            actual = f"cuda:{torch.cuda.current_device()}"
            if actual != requested or torch.cuda.device_count() != 1:
                return {"status": "error", "reason": "ocr_unavailable", "message": "device mismatch"}
            torch.cuda.synchronize()
        else:
            gpu = False
            device = "cpu"
            backend = "pytorch-cpu"
            actual = "cpu"
        reader = easyocr.Reader(
            list(request["languages"]), gpu=gpu, model_storage_directory=request["model_dir"],
            download_enabled=False, detector=True, recognizer=True, verbose=False,
        )
        detections = reader.readtext(rgb, detail=1, paragraph=False, workers=0)
        regions: list[dict[str, Any]] = []
        for order, detection in enumerate(detections):
            if order >= int(request["max_regions"]):
                return {"status": "error", "reason": "too_large", "message": "region count exceeds limit"}
            polygon, text, confidence = detection
            points = [[round(float(point[0])), round(float(point[1]))] for point in polygon]
            points = [[max(0, min(width, x)), max(0, min(height, y))] for x, y in points]
            xs, ys = zip(*points)
            regions.append({
                "text": str(text),
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                "polygon": points,
                "confidence": float(confidence),
                "paragraph": 0,
                "line": 0,
                "order": order,
            })
        model_hash = _model_fingerprint(
            request["model_dir"], request["languages"],
            str(request.get("qualification_manifest", "")),
        )
        return {
            "status": "success", "width": width, "height": height, "regions": regions,
            "device": device,
            "device_binding": os.environ.get("ROCR_VISIBLE_DEVICES", actual),
            "backend": backend,
            "engine_name": "easyocr", "engine_version": str(getattr(easyocr, "__version__", "unknown")),
            "model_fingerprint": model_hash, "pipeline_version": 1,
        }
    except Exception as exc:  # noqa: BLE001 - worker boundary returns safe reason
        memory_reason = _memory_failure_reason(exc, device)
        if memory_reason:
            return {"status": "error", "reason": memory_reason, "message": type(exc).__name__}
        return {"status": "error", "reason": "ocr_failed", "message": type(exc).__name__}


def _memory_failure_reason(exc: BaseException, device: Any) -> str | None:
    """Classify only clear allocator failures; unknown death stays terminal-safe."""
    text = f"{type(exc).__name__} {exc}".lower()
    if not any(marker in text for marker in ("out of memory", "outofmemory", "oom")):
        return None
    return "gpu_oom" if str(device).startswith("cuda") else "cpu_oom"


def _disable_network() -> None:
    """Make accidental model/network fallback fail closed inside the worker."""
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("OCR worker network access is disabled")

    socket.socket.connect = denied  # type: ignore[method-assign]
    socket.socket.connect_ex = denied  # type: ignore[method-assign]
    socket.create_connection = denied  # type: ignore[assignment]


def _model_fingerprint(model_dir: str, languages: Any, manifest_path: str = "") -> str:
    """Identify the audited model tree consistently in parent and worker.

    Qualification has already hashed every model file and verified that
    aggregate before this function runs.  Binding that aggregate to the
    normalized languages avoids another full model-tree scan and lets the
    parent derive the exact cache key before worker startup.
    """
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        aggregate = str(manifest["model_files"]["aggregate_sha256"])
        if len(aggregate) != 64:
            raise ValueError("invalid aggregate")
        int(aggregate, 16)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        # This path is unreachable after _verify_qualification succeeds, but a
        # deterministic fail-closed value keeps the helper safe in isolation.
        aggregate = hashlib.sha256(str(model_dir).encode()).hexdigest()
    normalized = sorted(str(language).casefold() for language in languages)
    return hashlib.sha256(json.dumps({
        "aggregate": aggregate.lower(), "languages": normalized,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _worker_main() -> int:
    import sys

    body = sys.stdin.buffer.read(_HEADER.size)
    if len(body) != _HEADER.size:
        return 2
    length = _HEADER.unpack(body)[0]
    if not 2 <= length <= _FRAME_LIMIT:
        return 2
    request = json.loads(sys.stdin.buffer.read(length).decode("utf-8"))
    _apply_memory_limit(request.get("memory_limit_mb"), device=request.get("device", "cpu"))
    result = _decode_and_ocr(request)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _FRAME_LIMIT:
        result = {"status": "error", "reason": "too_large", "message": "result exceeds limit"}
        encoded = json.dumps(result, separators=(",", ":")).encode()
    sys.stdout.buffer.write(_HEADER.pack(len(encoded)) + encoded)
    sys.stdout.buffer.flush()
    return 0


def _apply_memory_limit(value: Any, *, device: Any = "cpu") -> None:
    """Apply a best-effort per-worker address-space ceiling on Linux.

    GPU workers use the systemd cgroup ceiling instead.  An RLIMIT_AS ceiling
    also limits virtual address mappings, which can prevent ROCm's shared
    objects from loading even when physical VRAM and cgroup memory are safe.
    CPU workers retain this local guard for deploy-style launches.
    """
    if (os.name != "posix" or str(device).startswith("cuda") or
            not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        return
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        requested = value * 1024 * 1024
        ceiling = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(resource.RLIMIT_AS, (min(soft, ceiling), hard))
    except (ImportError, OSError, ValueError):
        log.debug("OCR worker memory limit could not be applied", exc_info=True)


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
