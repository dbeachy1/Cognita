"""Authorized PNG snapshotting, admission, layout normalization, and OCR API."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import stat
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..acceleration_profiles import effective_max_busy_percent
from ..gpu_probe import resolve_cards
from .limits import MAX_DIMENSION, MAX_PNG_BYTES
from .models import AssetError
from .ocr_models import OCRRegion, OCRWorkerPayload
from .ocr_store import normalize_languages_key
from .ocr_worker import OCRWorkerError, OCRWorkerRunner
from .png import scan_png

log = logging.getLogger("cognita.assets.ocr")

_RECOVERABLE_MEMORY_REASONS = frozenset({
    "gpu_oom", "cpu_oom", "gpu_admission_rejected", "cpu_memory_pressure",
})


def configured_pipeline_identity(config: Any, languages: Sequence[str]) -> tuple[str, str] | None:
    """Return the qualified model/pipeline fingerprints without starting OCR.

    Cache lookup must be possible before an expensive worker launch while still
    failing closed when the deployed qualification manifest is absent or
    malformed.  The worker derives the same model fingerprint from the
    manifest's audited aggregate plus the normalized language set.
    """
    manifest_path = str(getattr(config, "ocr_qualification_manifest", ""))
    if not manifest_path:
        return None
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        engine = str(manifest["engine"])
        engine_name, separator, engine_version = engine.partition("==")
        aggregate = str(manifest["model_files"]["aggregate_sha256"])
        if not separator or len(aggregate) != 64:
            return None
        int(aggregate, 16)
        normalized = normalize_languages_key(languages).split(",")
        model = hashlib.sha256(json.dumps({
            "aggregate": aggregate.lower(), "languages": normalized,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        pipeline = hashlib.sha256(json.dumps({
            "engine": engine_name, "version": engine_version,
            "model": model, "pipeline": 1, "languages": normalized,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return model, pipeline


class OCRRunner(Protocol):
    async def run(self, image: bytes, languages: tuple[str, ...], *, timeout_seconds: float | None = None) -> OCRWorkerPayload: ...


class OCRCapacityGate(Protocol):
    """Optional host/device admission seam supplied by the engine host."""

    async def qualify(self, *, device: str, phase: str, deadline: float) -> Any: ...

    async def wait(self, *, deadline: float) -> None: ...

    async def cleanup(self, *, device: str, reason: str) -> None: ...

    async def release(self, *, device: str) -> None: ...

    def attempt_devices(self) -> Sequence[str]: ...


class SchedulerOCRCapacityGate:
    """Fresh RAM/VRAM admission plus shared scheduler compute turns."""

    def __init__(self, config: Any, scheduler: Any, gpu_probe: Any = None) -> None:
        self.config = config
        self.scheduler = scheduler
        self.gpu_probe = gpu_probe
        self._held: dict[str, asyncio.Lock] = {}
        self._held_lock = asyncio.Lock()

    async def qualify(self, *, device: str, phase: str, deadline: float) -> bool:
        handler = self._handler(device)
        if handler is None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(handler._turn.acquire(), timeout=remaining)
        except TimeoutError:
            return False
        admitted = False
        try:
            started = getattr(self.scheduler, "external_turn_started", None)
            if started is not None:
                await started()
            if device == "cpu":
                admitted = _cpu_headroom_available(self.config)
            else:
                admitted = self._gpu_headroom_available(handler.device_id)
            if admitted:
                async with self._held_lock:
                    self._held[device] = handler._turn
            return admitted
        finally:
            if not admitted:
                handler._turn.release()
                finished = getattr(self.scheduler, "external_turn_finished", None)
                if finished is not None:
                    await finished()

    async def wait(self, *, deadline: float) -> None:
        await asyncio.sleep(min(5.0, max(0.001, deadline - time.monotonic())))

    async def cleanup(self, *, device: str, reason: str) -> None:
        # OCRWorkerRunner owns process termination and reaping before surfacing
        # an error. Capacity is not credited here; release happens explicitly.
        return None

    async def release(self, *, device: str) -> None:
        async with self._held_lock:
            turn = self._held.pop(device, None)
        if turn is not None and turn.locked():
            turn.release()
            finished = getattr(self.scheduler, "external_turn_finished", None)
            if finished is not None:
                await finished()

    def attempt_devices(self) -> tuple[str, ...]:
        """Return administrator-selected physical GPUs in stable attempt order."""
        if not str(getattr(self.config, "ocr_device", "cpu")).startswith(("gpu", "cuda")):
            return ("cpu",)
        try:
            devices = list(self.gpu_probe.devices()) if self.gpu_probe is not None else []
        except Exception:
            devices = []
        configured = list(getattr(self.config, "ocr_gpu_device_ids", ()) or ())
        singular = str(getattr(self.config, "ocr_gpu_device_id", ""))
        if not configured and singular:
            configured = [singular]
        selected: list[Any]
        if configured:
            by_binding = {
                _gpu_binding(device.unique_id).casefold(): device
                for device in devices if device.unique_id
            }
            selected = [
                by_binding[binding.casefold()]
                for binding in map(_gpu_binding, configured)
                if binding.casefold() in by_binding
            ]
        else:
            selection = resolve_cards(
                devices,
                getattr(self.config, "gpu_cards", "all"),
                getattr(self.config, "gpu_device_ids", None),
            )
            selected = (
                devices if selection.pinned is None
                else [device for device in devices if device.pci_address in selection.pinned]
            )
        attempts = tuple(
            _gpu_binding(device.unique_id)
            for device in selected if device.unique_id
        )
        return (*attempts, "cpu")

    def _handler(self, device: str) -> Any | None:
        if device == "cpu":
            return getattr(self.scheduler, "cpu", None)
        if device in {"gpu", "cuda"}:
            configured = list(getattr(self.config, "ocr_gpu_device_ids", ()) or ())
            singular = str(getattr(self.config, "ocr_gpu_device_id", ""))
            device = configured[0] if configured else singular
            if not device:
                return None
        try:
            devices = list(self.gpu_probe.devices()) if self.gpu_probe is not None else []
        except Exception:
            return None
        selected = next(
            (candidate for candidate in devices
             if candidate.unique_id and
             _gpu_binding(candidate.unique_id).casefold() == _gpu_binding(device).casefold()),
            None,
        )
        if selected is None:
            return None
        return next(
            (handler for handler in getattr(self.scheduler, "gpus", ())
             if handler.device_id == selected.sysfs_name and handler.state != "stopping"),
            None,
        )

    def _gpu_headroom_available(self, handler_id: str) -> bool:
        if self.gpu_probe is None:
            return False
        try:
            devices = list(self.gpu_probe.devices())
        except Exception:
            return False
        selected = next((device for device in devices if device.sysfs_name == handler_id), None)
        if selected is None:
            return False
        required_mb = int(getattr(self.config, "ocr_gpu_required_vram_mb", 4_096))
        reserve_mb = round(float(getattr(self.config, "gpu_reserve_vram_gb", 4.0)) * 1024)
        max_busy = effective_max_busy_percent(getattr(self.config, "gpu_max_busy_percent", None))
        if max_busy is not None and selected.busy_percent is not None and selected.busy_percent > max_busy:
            return False
        return selected.vram_free >= (required_mb + reserve_mb) * 1024 * 1024


class OCRService:
    """Perform one bounded OCR request against an already-authorized project."""

    _LANGUAGE_DEFAULT = ("en",)
    _LANGUAGE_ENABLED = frozenset({"en"})

    def __init__(self, config: Any | None = None, *, logger: Any | None = None,
                 runner: OCRRunner | None = None,
                 capacity_gate: OCRCapacityGate | None = None,
                 clock: Any | None = None) -> None:
        self.config = config
        self.log = logger
        self._runner = runner
        self._capacity_gate = capacity_gate or getattr(config, "ocr_capacity_gate", None)
        self._clock = clock or time.monotonic
        self._enabled_languages = frozenset(
            getattr(config, "ocr_enabled_languages", self._LANGUAGE_ENABLED)
        )
        concurrency = max(1, int(getattr(config, "ocr_concurrency", 1)))
        self._slots = asyncio.Semaphore(concurrency)
        self._admission_lock = asyncio.Lock()
        self._admitted = 0
        self._queue_size = max(0, int(getattr(config, "ocr_queue_size", 4)))
        self._coalesce_lock = asyncio.Lock()
        self._coalesced: dict[tuple[Any, ...], list[Any]] = {}

    async def coalesce(
        self, key: tuple[Any, ...], factory: Any, *, timeout_seconds: float,
    ) -> Any:
        """Share one computation while retaining independent waiter ownership."""
        async with self._coalesce_lock:
            entry = self._coalesced.get(key)
            if entry is None or entry[0].done():
                task = asyncio.create_task(factory(), name="cognita-ocr-coalesced")
                entry = [task, 0]
                self._coalesced[key] = entry
            entry[1] += 1
            task = entry[0]
        try:
            # A short-lived caller cannot cancel work still owned by another
            # authorized waiter.  Cleanup below cancels only the final owner.
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, timeout_seconds))
        finally:
            cancel = False
            async with self._coalesce_lock:
                current = self._coalesced.get(key)
                if current is entry:
                    entry[1] -= 1
                    if entry[1] == 0:
                        self._coalesced.pop(key, None)
                        cancel = not task.done()
            if cancel:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @property
    def runner(self) -> OCRRunner:
        if self._runner is None:
            self._runner = OCRWorkerRunner(
                self.config,
                device=str(getattr(self.config, "ocr_device", "cpu")),
                device_id=str(getattr(self.config, "ocr_gpu_device_id", "")) or None,
            )
        return self._runner

    async def extract(self, project: Any, filepath: str, *, languages: Sequence[str] | None = None,
                      target: Path | None = None, timeout_seconds: Any = None,
                      outer_deadline: float | None = None,
                      started_at: float | None = None) -> dict[str, Any]:
        started = self._clock() if started_at is None else float(started_at)
        selected_timeout = _ocr_timeout_seconds(timeout_seconds)
        deadline = started + selected_timeout
        if outer_deadline is not None:
            deadline = min(deadline, float(outer_deadline))
        selected = self._languages(languages)
        if target is None:
            raise AssetError("invalid_path", "OCR requires AssetService path authorization")
        admitted = await self._admit()
        if not admitted:
            raise AssetError("busy", "OCR admission queue is full")
        try:
            remaining = max(0.001, deadline - self._clock())
            await asyncio.wait_for(self._slots.acquire(), timeout=remaining)
        except TimeoutError as exc:
            await self._leave_admission()
            raise self._timeout_error("admission", selected_timeout, outer_deadline=outer_deadline) from exc
        except asyncio.CancelledError:
            await self._leave_admission()
            raise
        try:
            self._ensure_time(deadline, "source_validation", selected_timeout)
            snapshot, facts, image = self._snapshot(target, filepath)
            del image

            def load_image() -> bytes:
                current, _facts, current_image = self._snapshot(target, filepath)
                if current["source_sha256"] != snapshot["source_sha256"]:
                    raise AssetError("source_changed", "asset changed while awaiting OCR capacity")
                return current_image

            payload, fallback_reason = await self._run_with_recovery(
                load_image, selected, deadline, selected_timeout,
            )
            result = self._result(project, filepath, snapshot, facts, payload, selected,
                                  started=started)
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > int(
                getattr(self.config, "ocr_max_result_bytes", 1_048_576)
            ):
                raise AssetError("too_large", "OCR result exceeds the configured output limit")
            self._ensure_time(deadline, "publication", selected_timeout)
            self._freshness_check(target, snapshot)
            if fallback_reason:
                result["warnings"].append({
                    "code": "gpu_fallback",
                    "message": "OCR completed on CPU after GPU capacity pressure",
                    "reason": fallback_reason,
                })
            return result
        except AssetError:
            raise
        except asyncio.CancelledError:
            raise
        except OCRWorkerError as exc:
            if exc.reason in {"ocr_unavailable", "wrong_media_type", "animated_png", "too_large"}:
                # exc.message is set only for the missing-weights case (the sentence tells the operator
                # what to do); every other worker diagnostic stays out of the reply.
                raise AssetError(exc.reason, exc.message or "OCR worker is unavailable for this request") from exc
            raise AssetError("ocr_failed", "OCR inference failed") from exc
        except TimeoutError as exc:
            raise self._timeout_error("inference", selected_timeout) from exc
        except Exception as exc:
            raise AssetError("ocr_failed", "OCR inference failed") from exc
        finally:
            self._slots.release()
            await self._leave_admission()

    async def _run_with_recovery(self, load_image: Callable[[], bytes], languages: tuple[str, ...],
                                 deadline: float, limit_seconds: int) -> tuple[OCRWorkerPayload, str | None]:
        """Run OCR with capacity gates while retaining no image bytes in the wait queue."""
        configured = str(getattr(self.config, "ocr_device", "cpu"))
        gpu = configured.startswith(("gpu", "cuda"))
        provider = getattr(self._capacity_gate, "attempt_devices", None)
        devices = list(provider()) if gpu and provider is not None else [configured]
        if gpu and "cpu" not in devices:
            devices.append("cpu")
        cooldowns: dict[str, float] = {}
        fallback_reason: str | None = (
            "gpu_admission_rejected"
            if gpu and not any(device != "cpu" for device in devices)
            else None
        )
        phase = "worker_startup"
        while True:
            self._ensure_time(deadline, "capacity_wait", limit_seconds)
            now = self._clock()
            for device in devices:
                if now < cooldowns.get(device, 0.0):
                    continue
                admitted = await self._qualify(device, phase, deadline)
                if not admitted:
                    if device != "cpu":
                        fallback_reason = fallback_reason or "gpu_admission_rejected"
                    continue
                try:
                    image = load_image()
                    try:
                        remaining = max(0.001, deadline - self._clock())
                        payload = await self._invoke_runner(
                            image, languages, remaining, device=device,
                        )
                        return payload, fallback_reason if device == "cpu" else None
                    finally:
                        del image
                except OCRWorkerError as exc:
                    if exc.reason not in _RECOVERABLE_MEMORY_REASONS:
                        raise
                    await self._cleanup_device(device, exc.reason)
                    cooldowns[device] = self._clock() + 30.0
                    if device != "cpu" and fallback_reason is None and exc.reason:
                        fallback_reason = exc.reason
                    phase = "inference"
                    continue
                except TimeoutError:
                    raise
                finally:
                    await self._release_device(device)
            if self._clock() < deadline:
                await self._wait_capacity(deadline, limit_seconds)

    async def _qualify(self, device: str, phase: str, deadline: float) -> bool:
        if self._capacity_gate is None:
            # GPU callers without a physical-device arbiter must not claim
            # unknown VRAM is available. CPU production still fails closed on
            # unknown host/cgroup headroom. Portable fake-runner tests
            # explicitly disable the KEI O_NOATIME requirement.
            if device != "cpu":
                return False
            if not bool(getattr(self.config, "ocr_require_noatime", True)):
                return True
            return _cpu_headroom_available(self.config)
        qualify = getattr(self._capacity_gate, "qualify", None)
        if qualify is None:
            return False
        result = await qualify(device=device, phase=phase, deadline=deadline)
        return _gate_admitted(result)

    async def _wait_capacity(self, deadline: float, limit_seconds: int) -> None:
        self._ensure_time(deadline, "capacity_wait", limit_seconds)
        waiter = getattr(self._capacity_gate, "wait", None) if self._capacity_gate is not None else None
        if waiter is not None:
            await waiter(deadline=deadline)
            return
        await asyncio.sleep(min(1.0, max(0.001, deadline - self._clock())))

    async def _cleanup_device(self, device: str, reason: str) -> None:
        cleanup = getattr(self._capacity_gate, "cleanup", None) if self._capacity_gate is not None else None
        if cleanup is not None:
            await cleanup(device=device, reason=reason)

    async def _release_device(self, device: str) -> None:
        release = getattr(self._capacity_gate, "release", None) if self._capacity_gate is not None else None
        if release is not None:
            await release(device=device)

    async def _invoke_runner(self, image: bytes, languages: tuple[str, ...], remaining: float, *, device: str) -> OCRWorkerPayload:
        run = self.runner.run
        try:
            parameters = inspect.signature(run).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "timeout_seconds" in parameters:
            kwargs["timeout_seconds"] = remaining
        if "device" in parameters:
            kwargs["device"] = "cpu" if device == "cpu" else "gpu"
        if "device_id" in parameters:
            binding = None
            if device != "cpu" and device not in {"gpu", "cuda"}:
                binding = _gpu_binding(device)
            kwargs["device_id"] = binding
        operation = run(image, languages, **kwargs)
        return await asyncio.wait_for(operation, timeout=remaining)

    def _ensure_time(self, deadline: float, phase: str, limit_seconds: int) -> None:
        if self._clock() >= deadline:
            raise self._timeout_error(phase, limit_seconds)

    @staticmethod
    def _timeout_error(phase: str, limit_seconds: int, *, outer_deadline: float | None = None) -> AssetError:
        details: dict[str, Any] = {"phase": phase, "limit_seconds": limit_seconds}
        if outer_deadline is not None:
            details["outer_deadline_clipped"] = True
        return AssetError("timeout", f"OCR deadline exceeded phase={phase} limit_seconds={limit_seconds}", details=details)

    async def _admit(self) -> bool:
        async with self._admission_lock:
            capacity = max(1, int(getattr(self.config, "ocr_concurrency", 1))) + self._queue_size
            if self._admitted >= capacity:
                return False
            self._admitted += 1
            return True

    async def _leave_admission(self) -> None:
        async with self._admission_lock:
            self._admitted = max(0, self._admitted - 1)

    def _languages(self, languages: Sequence[str] | None) -> tuple[str, ...]:
        if languages is None:
            languages = self._LANGUAGE_DEFAULT
        if not isinstance(languages, Sequence) or isinstance(languages, (str, bytes)):
            raise AssetError("unsupported_language", "languages must be a list")
        try:
            key = normalize_languages_key(languages)
        except ValueError:
            raise AssetError("unsupported_language", "languages must not repeat")
        canonical = tuple(key.split(","))
        if any(item not in self._enabled_languages for item in canonical):
            raise AssetError("unsupported_language", "requested OCR language is not enabled")
        return canonical

    def _snapshot(self, target: Path, filepath: str) -> tuple[dict[str, Any], Any, bytes]:
        max_bytes = min(int(getattr(self.config, "ocr_max_png_bytes", 16 * 1024 * 1024)), MAX_PNG_BYTES)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        noatime = getattr(os, "O_NOATIME", 0)
        require_noatime = bool(getattr(self.config, "ocr_require_noatime", True))
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif require_noatime:
            raise AssetError("ocr_unavailable", "descriptor no-symlink reads are unavailable")
        if noatime:
            flags |= noatime
        elif require_noatime:
            raise AssetError("ocr_unavailable", "timestamp-preserving OCR reads are unavailable")
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            if nofollow and exc.errno in {40, 62}:  # ELOOP on POSIX variants
                raise AssetError("invalid_path", "asset paths may not be links") from exc
            if noatime and require_noatime and exc.errno in {1, 22, 95}:  # EPERM/EINVAL/ENOTSUP
                raise AssetError("ocr_unavailable", "timestamp-preserving OCR reads are unavailable") from exc
            raise AssetError("not_found", "asset could not be read") from exc
        try:
            before = os.fstat(descriptor)
            if not _regular_file(before):
                raise AssetError("not_found", "asset is not a regular file")
            data = bytearray()
            while len(data) <= max_bytes:
                block = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise AssetError("not_found", "asset could not be read") from exc
        finally:
            os.close(descriptor)
        if len(data) > max_bytes:
            raise AssetError("too_large", "PNG exceeds the OCR byte limit")
        if _stat_token(before, include_atime=False) != _stat_token(after, include_atime=False) or len(data) != before.st_size:
            raise AssetError("source_changed", "asset changed while it was being read")
        if _stat_token(before, include_atime=require_noatime) != _stat_token(after, include_atime=require_noatime):
            # 2026-09-28, Windows 13.7.0 self-test: the first read of a PNG on
            # the OneDrive DrvFS mount set its atime AND ctime to the moment of
            # the read (Windows' lazy last-access update; O_NOATIME does not
            # reach NTFS through 9p), and this check returned source_changed
            # for an untouched file.  A timestamp is not evidence that content
            # changed: identity, size and mtime held, and the freshness check
            # re-hashes the bytes before any result is published.
            log.info("OCR source access timestamps moved during the read; content identity unchanged filepath=%s",
                     filepath)
        raw = bytes(data)
        try:
            facts, _ = scan_png(
                raw,
                byte_limit=max_bytes,
                dimension_limit=effective_dimension_limit(self.config),
                pixel_limit=min(int(getattr(self.config, "ocr_max_pixels", 16_777_216)), 16_777_216),
            )
        except AssetError as exc:
            if exc.reason == "animated_png":
                raise
            if exc.reason in {"byte_limit", "dimension_limit"}:
                raise AssetError("too_large", exc.message) from exc
            raise AssetError("invalid_png", exc.message) from exc
        token = _stat_token(before, include_atime=require_noatime)
        snapshot = {
            "filepath": filepath,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "file_size": len(raw),
            "width": facts.width,
            "height": facts.height,
            "snapshot_token": token,
            "atime_preserved": require_noatime,
        }
        return snapshot, facts, raw

    def _freshness_check(self, target: Path, snapshot: Mapping[str, Any]) -> None:
        try:
            current = target.stat(follow_symlinks=False)
        except OSError as exc:
            raise AssetError("source_changed", "asset disappeared during OCR") from exc
        if not _regular_file(current):
            raise AssetError("source_changed", "asset was replaced by a non-file")
        # Identity, size and mtime only (see _snapshot): the host may move
        # atime/ctime on a read Cognita made, and the hash below is the check
        # that decides whether the bytes changed.
        if _stat_token(current, include_atime=False) != _identity_part(str(snapshot["snapshot_token"])):
            raise AssetError("source_changed", "asset changed during OCR")
        # Hashing a replacement with unchanged stat fields is required by the
        # contract; the bounded read repeats the same descriptor discipline.
        _, current_hash = _read_hash(
            target, int(snapshot["file_size"]),
            require_noatime=bool(getattr(self.config, "ocr_require_noatime", True)),
        )
        if current_hash != snapshot["source_sha256"]:
            raise AssetError("source_changed", "asset bytes changed during OCR")

    def _result(self, project: Any, filepath: str, snapshot: Mapping[str, Any], facts: Any,
                payload: OCRWorkerPayload, languages: tuple[str, ...], *, started: float) -> dict[str, Any]:
        if payload.width != facts.width or payload.height != facts.height:
            raise AssetError("ocr_failed", "OCR dimensions do not match the source snapshot")
        regions = _normalize_regions(payload.regions, payload.width, payload.height)
        if len(regions) > int(getattr(self.config, "ocr_max_regions", 10_000)):
            raise AssetError("too_large", "OCR region count exceeds the configured limit")
        text = _layout_text(regions)
        warnings: list[dict[str, Any]] = []
        low = float(getattr(self.config, "ocr_low_confidence", 0.60))
        low_orders = [region.order for region in regions if region.confidence is not None and region.confidence < low]
        if low_orders:
            warnings.append({"code": "low_confidence", "message": "one or more regions are below the confidence threshold", "regions": low_orders})
        if regions and not text:
            warnings.append({"code": "partial_text", "message": "detected regions did not yield searchable text"})
        outcome = "text" if text else "no_text"
        searchable = bool(text)
        pipeline = hashlib.sha256(json.dumps({
            "engine": payload.engine_name, "version": payload.engine_version,
            "model": payload.model_fingerprint, "pipeline": payload.pipeline_version,
            "languages": list(languages),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = {
            "status": "success", "outcome": outcome,
            "project": getattr(project, "name", str(project)), "filepath": filepath,
            "sha256": snapshot["source_sha256"], "width": facts.width, "height": facts.height,
            "text": text, "regions": [region.as_dict() for region in regions],
            "engine": {"name": payload.engine_name, "version": payload.engine_version,
                        "model_fingerprint": payload.model_fingerprint,
                        "pipeline_version": payload.pipeline_version,
                        "device": payload.device, "device_binding": payload.device_binding,
                        "backend": payload.backend},
            "languages": list(languages), "cache_hit": False, "searchable": searchable,
            "warnings": warnings,
            "duration_ms": max(0, round((self._clock() - started) * 1000)),
            "limits": {
                "max_png_bytes": min(int(getattr(self.config, "ocr_max_png_bytes", 16 * 1024 * 1024)), MAX_PNG_BYTES),
                "max_pixels": int(getattr(self.config, "ocr_max_pixels", 16_777_216)),
                "max_dimension": effective_dimension_limit(self.config),
                "max_regions": int(getattr(self.config, "ocr_max_regions", 10_000)),
                "max_result_bytes": int(getattr(self.config, "ocr_max_result_bytes", 1_048_576)),
            },
            "source_snapshot": dict(snapshot), "pipeline_fingerprint": pipeline,
        }
        return result


def effective_dimension_limit(config: Any) -> int:
    """The per-side pixel limit OCR actually enforces.

    ``ocr_max_dimension`` defaults to 8,192 but DESIGN-10.0 §limits says it is
    "additionally limited by existing asset access bounds", and the pre-scan
    has always clamped to the asset store's 4,096 (``limits.MAX_DIMENSION``).
    Until 13.0.2 only the scan used the clamp: the refusal message said 4,096
    while every successful result's ``limits.max_dimension`` reported the raw
    8,192, and the worker was handed 8,192 too. One value, computed here, goes
    to the scan, the worker request and both ``limits`` blocks.
    """
    return min(int(getattr(config, "ocr_max_dimension", 8_192)), MAX_DIMENSION)


def source_snapshot_from_result(result: Mapping[str, Any]) -> Any:
    """Adapt the wire result to the persistence worker's approved snapshot DTO."""
    from .ocr_store import OcrSourceSnapshot

    snapshot = result.get("source_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("OCR result has no source snapshot")  # noqa: TRY004 - adapter contract
    return OcrSourceSnapshot(
        filepath=str(snapshot["filepath"]), source_sha256=str(snapshot["source_sha256"]),
        file_size=int(snapshot["file_size"]), width=int(snapshot["width"]),
        height=int(snapshot["height"]), snapshot_token=str(snapshot["snapshot_token"]),
    )


def result_facts_from_result(result: Mapping[str, Any]) -> Any:
    """Adapt the public result to ``OcrResultFacts`` without duplicating it."""
    from .ocr_store import OcrResultFacts, normalize_languages_key

    engine = result.get("engine")
    if not isinstance(engine, Mapping):
        raise ValueError("OCR result has no engine manifest")  # noqa: TRY004 - adapter contract
    return OcrResultFacts(
        source_sha256=str(result["sha256"]),
        pipeline_fingerprint=str(result["pipeline_fingerprint"]),
        languages_key=normalize_languages_key(result.get("languages", [])),
        engine_name=str(engine["name"]), engine_version=str(engine["version"]),
        model_fingerprint=str(engine["model_fingerprint"]),
        pipeline_version=int(engine["pipeline_version"]),
        width=int(result["width"]), height=int(result["height"]),
        outcome=str(result["outcome"]), text=str(result["text"]),
        regions=list(result["regions"]), warnings=list(result["warnings"]),
        device=str(engine["device"]), backend=str(engine["backend"]),
    )


def _regular_file(stat_result: os.stat_result) -> bool:
    return stat.S_ISREG(stat_result.st_mode)


def _identity_part(token: str) -> str:
    """The dev:ino:size:mtime prefix of a stored snapshot token."""
    return ":".join(token.split(":")[:4])


def _stat_token(stat_result: os.stat_result, *, include_atime: bool = True) -> str:
    # The production path requires O_NOATIME and therefore compares ctime and
    # atime.  Tests and non-KEI development hosts may explicitly opt out of
    # timestamp preservation; that compatibility mode compares identity,
    # size, and mtime while making its limitation visible in the snapshot.
    names = ["st_dev", "st_ino", "st_size", "st_mtime_ns"]
    if include_atime:
        names.extend(("st_ctime_ns", "st_atime_ns"))
    return ":".join(str(getattr(stat_result, name, 0)) for name in names)


def _read_hash(target: Path, size: int, *, require_noatime: bool) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if noatime:
        flags |= noatime
    elif require_noatime:
        raise AssetError("ocr_unavailable", "timestamp-preserving OCR reads are unavailable")
    if nofollow:
        flags |= nofollow
    elif require_noatime:
        raise AssetError("ocr_unavailable", "descriptor no-symlink reads are unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        digest = hashlib.sha256()
        count = 0
        while count <= size:
            block = os.read(descriptor, min(1024 * 1024, size + 1 - count))
            if not block:
                break
            digest.update(block)
            count += len(block)
        return count, digest.hexdigest()
    except OSError as exc:
        raise AssetError("source_changed", "asset could not be revalidated") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _normalize_regions(regions: Sequence[OCRRegion], width: int, height: int) -> tuple[OCRRegion, ...]:
    bounded: list[OCRRegion] = []
    for region in regions:
        text = unicodedata.normalize("NFC", region.text).replace("\r\n", "\n").replace("\r", "\n")
        polygon = tuple((max(0, min(width, x)), max(0, min(height, y))) for x, y in region.polygon)
        if not polygon:
            continue
        xs, ys = zip(*polygon)
        bounded.append(OCRRegion(text, (min(xs), min(ys), max(xs), max(ys)), polygon,
                                 region.confidence, 0, 0, 0))
    ordered = sorted(bounded, key=lambda region: (region.bbox[1], region.bbox[0], region.bbox[3], region.bbox[2], region.text))
    output: list[OCRRegion] = []
    line_bottom = -1
    line_top = -1
    line = -1
    for region in ordered:
        y0, y1 = region.bbox[1], region.bbox[3]
        overlap = max(0, min(line_bottom, y1) - max(line_top, y0))
        if line < 0 or overlap <= 0:
            line += 1
            line_top, line_bottom = y0, y1
        else:
            line_top = min(line_top, y0)
            line_bottom = max(line_bottom, y1)
        output.append(OCRRegion(region.text, region.bbox, region.polygon, region.confidence, 0, line, len(output)))
    return tuple(output)


def _layout_text(regions: Sequence[OCRRegion]) -> str:
    if not regions:
        return ""
    lines: dict[int, list[OCRRegion]] = {}
    for region in regions:
        lines.setdefault(region.line, []).append(region)
    return "\n".join(" ".join(item.text for item in sorted(items, key=lambda value: value.bbox[0])) for _, items in sorted(lines.items()))


def _ocr_timeout_seconds(value: Any) -> int:
    """Validate the only caller-visible OCR patience control."""
    if value is None:
        return 120
    # bool is an int subclass, but accepting it would make ``true`` mean a
    # one-second deadline and would violate the wire contract.
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 600:
        raise AssetError("invalid_arguments", "timeout_seconds must be an integer between 10 and 600")
    return value


def _gate_admitted(value: Any) -> bool:
    """Normalize the shared gate's bounded result without trusting telemetry."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("admitted", "qualified", "available"):
            if key in value:
                return value[key] is True
        return False
    for key in ("admitted", "qualified", "available"):
        marker = getattr(value, key, None)
        if marker is not None:
            return marker is True
    return False


def _gpu_binding(value: Any) -> str:
    raw = str(value)
    return raw if raw.startswith("GPU-") else f"GPU-{raw}"


def _cpu_headroom_available(config: Any, *, proc_root: Path = Path("/proc"),
                            cgroup_root: Path = Path("/sys/fs/cgroup"),
                            platform: str | None = None) -> bool:
    """Apply the fail-closed Linux host-RAM and cgroup admission gate."""
    if (platform or os.name) != "posix":
        return False
    try:
        fields: dict[str, int] = {}
        for line in (proc_root / "meminfo").read_text(encoding="ascii").splitlines():
            name, separator, raw = line.partition(":")
            if separator and raw.strip().endswith(" kB"):
                fields[name] = int(raw.strip().split()[0]) * 1024
        available = fields.get("MemAvailable")
        if available is None:
            return False
        required = (
            int(getattr(config, "ocr_worker_memory_mb", 2_048))
            + int(getattr(config, "ocr_cpu_reserve_ram_mb", 1_024))
        ) * 1024 * 1024
        allowance = available
        maximum_path = cgroup_root / "memory.max"
        current_path = cgroup_root / "memory.current"
        if maximum_path.is_file() and current_path.is_file():
            maximum_text = maximum_path.read_text(encoding="ascii").strip()
            if maximum_text != "max":
                allowance = min(
                    allowance,
                    max(0, int(maximum_text) - int(current_path.read_text(encoding="ascii").strip())),
                )
        return allowance >= required
    except (OSError, TypeError, ValueError):
        return False
