"""Bounded, image-owned acceleration verification.

The service interpreter is intentionally CPU-only.  This module therefore
delegates embedding and OCR execution to the already isolated worker runtimes,
and returns only bounded, non-secret facts to the Admin acceleration store.
Tests inject a verifier instead of importing either optional GPU runtime.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import struct
import time
import zlib
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from .acceleration_profiles import gpu_settings_profile, profile_named
from .assets.ocr_worker import OCRWorkerError, OCRWorkerRunner
from .gpu_host import CANARY_TEXT, GpuUnavailable, GpuWorker
from .gpu_probe import GpuDevice, GpuProbe, batch_ceiling_gb, gate_devices

log = logging.getLogger("cognita.acceleration.verify")

VERIFY_DEADLINE_SECONDS = 120.0


class AccelerationVerifier(Protocol):
    def verify(
        self,
        *,
        policy: Any,
        cards: list[GpuDevice],
        profile: str,
        deadline: float,
    ) -> dict[str, Any]: ...


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    return max(0.0, deadline - clock())


def _probe_png() -> bytes:
    """Return a deterministic one-pixel PNG for an OCR load/inference canary."""
    raw = b"\x00\xff\xff\xff\xff"
    body = b"\x89PNG\r\n\x1a\n"
    body += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    body += _png_chunk(b"IDAT", zlib.compress(raw, level=9))
    body += _png_chunk(b"IEND", b"")
    return body


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _safe_category(exc: BaseException, *, deadline: float, clock: Callable[[], float]) -> str:
    # 15.0 (DESIGN-NVIDIA §7): the worker itself named the cause ("worker
    # construction failed reason=driver_too_old") and `GpuWorker.start()` carried
    # it in `GpuUnavailable.reason`. A cause the worker stated outright beats any
    # guess made from the message text, so it is checked FIRST, ahead of the
    # substring mapping below (whose "provider"/"cpu" test would otherwise file
    # ORT's "CUDA driver version is insufficient" text under provider_cpu_fallback
    # or runtime_missing).
    if getattr(exc, "reason", None) == "driver_too_old":
        log.info("acceleration verification category=driver_too_old source=worker_reason")
        return "driver_too_old"
    if _remaining(deadline, clock) <= 0:
        return "verification_timeout"
    if isinstance(exc, TimeoutError):
        return "verification_timeout"
    if isinstance(exc, OCRWorkerError):
        reason = str(exc)
        return {
            "gpu_oom": "insufficient_vram",
            "model_unqualified": "model_unqualified",
            "ocr_unavailable": "runtime_missing",
            "ocr_failed": "canary_failed",
        }.get(reason, "model_unqualified")
    if isinstance(exc, GpuUnavailable):
        text = str(exc).lower()
        if "timeout" in text or "wedged" in text:
            return "verification_timeout"
        if "provider" in text or "cpu" in text:
            return "provider_cpu_fallback"
        return "runtime_missing"
    return "runtime_missing"


class RealAccelerationVerifier:
    """Run one bounded provider/canary check per selected card.

    A verifier instance owns no persistent worker. Every call starts fresh
    workers and reaps them before returning, so a failed verification cannot
    reserve VRAM or alter the configured/loaded policy.
    """

    def __init__(
        self,
        config: Any | None,
        probe: GpuProbe,
        *,
        clock: Callable[[], float] = time.monotonic,
        worker_factory: Callable[[GpuDevice, Any, GpuProbe], Any] = GpuWorker,
        ocr_factory: Callable[..., OCRWorkerRunner] = OCRWorkerRunner,
    ) -> None:
        self.config = config
        self.probe = probe
        self.clock = clock
        self.worker_factory = worker_factory
        self.ocr_factory = ocr_factory
        self.cpu_embedder: Callable[[list[str]], Any] | None = None

    def set_cpu_embedder(self, embedder: Callable[[list[str]], Any]) -> None:
        """Attach the service's already-owned CPU embedder after startup."""
        self.cpu_embedder = embedder

    def verify(self, *, policy: Any, cards: list[GpuDevice], profile: str, deadline: float) -> dict[str, Any]:
        selected_knowledge = self._selected_knowledge(policy, cards)
        selected_ocr = self._selected_ocr(policy, cards)
        # 15.0: the profile decides both whether a GPU check runs and which
        # provider it must find. `gpu_settings_profile` gives a `cpu` profile the
        # `amd` row's provider (the pre-15.0 literal) so the not-requested result
        # below reports what it always did.
        prof = profile_named(profile)
        expected_provider = gpu_settings_profile(prof).embed_provider
        log.info(
            "acceleration verification start profile=%s gpu=%s expected_provider=%s cards=%d",
            prof.name, prof.gpu, expected_provider, len(cards),
        )
        result: dict[str, Any] = {
            "embedding": {"ready": False, "provider": expected_provider, "active_providers": [], "canary": {"state": "not_requested", "delta": None}, "cards": []},
            "ocr": {"ready": False, "qualification": "not_requested", "cards": []},
            "cards": {},
            "cleanup": "passed",
        }
        if not prof.gpu:
            result["embedding"]["reason"] = "profile_cpu"
            result["ocr"]["reason"] = "profile_cpu"
            return result
        if policy.knowledge.gpu_enabled:
            usable, skipped = self._split_by_vram(selected_knowledge, explicit=bool(policy.knowledge.gpu_device_ids))
            result["embedding"] = self._verify_embedding(usable, deadline, expected_provider)
            self._add_skipped(result["embedding"], "embedding", skipped)
        if policy.ocr.device == "gpu":
            usable, skipped = self._split_by_vram(selected_ocr, explicit=bool(policy.ocr.gpu_device_ids))
            result["ocr"] = self._verify_ocr(usable, deadline)
            self._add_skipped(result["ocr"], "ocr", skipped)
        if _remaining(deadline, self.clock) <= 0:
            # A worker can return a successful frame exactly as the deadline
            # expires.  Do not publish that late result as usable.
            for component in (result["embedding"], result["ocr"]):
                for row in component.get("cards", []):
                    if row.get("state") == "passed":
                        row["state"] = "failed"
                        row["reason"] = "verification_timeout"
                if component.get("cards"):
                    component["ready"] = False
                    component["reason"] = "verification_timeout"
        for row in result["embedding"].get("cards", []) + result["ocr"].get("cards", []):
            pci = row.get("pci_address")
            if pci:
                slot = result["cards"].setdefault(pci, {"embedding": None, "ocr": None})
                slot[row["component"]] = row
        for component in (result["embedding"], result["ocr"]):
            # A skipped card was never started, so it neither passes nor fails
            # the check; at least one card must actually have passed.
            tried = [row for row in component.get("cards", []) if row["state"] != "skipped"]
            component["ready"] = bool(tried) and all(row["state"] == "passed" for row in tried)
        if any(row.get("cleanup") != "passed" for component in (result["embedding"], result["ocr"]) for row in component.get("cards", []) if row["state"] != "skipped"):
            result["cleanup"] = "failed"
            log.error("acceleration verification category=worker_cleanup_failed")
        return result

    def _selected_knowledge(self, policy: Any, cards: list[GpuDevice]) -> list[GpuDevice]:
        ids = set(policy.knowledge.gpu_device_ids)
        return [card for card in cards if not ids or card.pci_address in ids]

    def _selected_ocr(self, policy: Any, cards: list[GpuDevice]) -> list[GpuDevice]:
        ids = set(policy.ocr.gpu_device_ids)
        return [card for card in cards if not ids or (card.unique_id and f"GPU-{card.unique_id}" in ids)]

    def _split_by_vram(self, cards: list[GpuDevice], *, explicit: bool) -> tuple[list[GpuDevice], list[GpuDevice]]:
        """With no card list chosen, check only the cards the runtime would use.

        2026-09-29, first install on kei: "all cards" also meant the display
        card with 0.98 GB free.  Indexing never uses it (the VRAM gate in
        ``gpu_probe.gate_devices`` rejects it), but verification started a
        worker on it, HIP bound a different card, the worker refused to run,
        and the whole check failed although both real cards passed.  So the
        VRAM gate decides here, but on the MODEL'S OWN ceiling only: no
        `gpu_reserve_vram_gb` headroom and no busy limit.  Those protect other
        apps per indexing job, which re-checks every time; this decision is
        permanent (a failed check re-stages the CPU image), so a moment of
        ComfyUI or llama.cpp load must not turn a capable card into a CPU
        install (final review, 2026-09-29).  The display card still fails it.
        Cards the user named explicitly are always tried: they asked for them.
        """
        if explicit or not cards:
            return list(cards), []
        results = gate_devices(
            cards,
            batch_ceiling_gb=batch_ceiling_gb(int(getattr(self.config, "gpu_batch_size", 4) or 4)),
            reserve_vram_gb=0.0,
            max_busy_percent=100,
        )
        usable = [r.device for r in results if r.qualifies]
        skipped = [r.device for r in results if not r.qualifies]
        for r in results:
            if not r.qualifies:
                log.info("acceleration verification skip pci=%s reason=%s", r.device.pci_address, r.reason)
        return usable, skipped

    @staticmethod
    def _add_skipped(component: dict[str, Any], name: str, skipped: list[GpuDevice]) -> None:
        for card in skipped:
            component["cards"].append({"component": name, "pci_address": card.pci_address, "state": "skipped", "reason": "insufficient_vram", "cleanup": "not_started"})
        if skipped and component.get("reason") == "configured_card_missing" and not any(row["state"] != "skipped" for row in component["cards"]):
            # Cards exist; none has room for the model.
            component["reason"] = "insufficient_vram"

    def _worker_config(self, remaining: float) -> Any:
        source = self.config
        values = {
            "gpu_venv_python": "/opt/cognita-runtimes/embed/bin/python",
            "embedding_model": "BAAI/bge-large-en-v1.5",
            "gpu_model_cache_dir": "/var/lib/cognita/models",
            "models_cache_dir": "/var/lib/cognita/models",
            # 15.0: the three provider-shaped settings are the config's own, passed
            # THROUGH untouched, so verification runs with exactly the settings
            # indexing uses. These defaults apply only when there is no config
            # (or it lacks the key) and they mean "the profile's": "" provider,
            # -1 sequence length, no program cache. `GpuWorker.start()` resolves
            # them to concrete values in ONE place. (Before 15.0 this hardcoded
            # "migraphx" and 512, which on an NVIDIA profile would have verified
            # a shape indexing never runs.)
            "gpu_provider": "",
            "gpu_batch_size": 4,
            "gpu_fixed_seq_len": -1,
            "embedding_dimensions": 1024,
            "gpu_program_cache_dir": "",
            "gpu_worker_shutdown_s": min(2.0, max(0.1, remaining)),
            "gpu_worker_startup_timeout_s": max(0.1, remaining),
        }
        if source is not None:
            for key in tuple(values):
                if hasattr(source, key):
                    values[key] = getattr(source, key)
        return SimpleNamespace(**values)

    def _cpu_reference(self, deadline: float) -> list[float] | None:
        """Use the already-owned service embedder when the engine is ready.

        The verifier never constructs a second CPU model: doing so would make
        Admin verification compete with indexing and could trigger an
        unbounded model load.  The service wires its loaded ``Embedder`` into
        this hook during engine construction.  Hardware-free callers may
        leave it unset; the live provider/vector-shape canary still runs.
        """
        reference_fn = self.cpu_embedder or getattr(self.config, "acceleration_cpu_embedder", None)
        if reference_fn is None:
            return None
        if _remaining(deadline, self.clock) <= 0:
            raise TimeoutError("verification deadline exceeded before CPU canary")
        vectors = reference_fn([CANARY_TEXT])
        reference = vectors[0]
        return reference.tolist() if hasattr(reference, "tolist") else list(reference)

    def _verify_embedding(self, cards: list[GpuDevice], deadline: float, expected_provider: str) -> dict[str, Any]:
        result: dict[str, Any] = {"ready": False, "provider": expected_provider, "active_providers": [], "canary": {"state": "failed", "delta": None}, "cards": []}
        if not cards:
            result["reason"] = "configured_card_missing"
            return result
        try:
            reference = self._cpu_reference(deadline)
        except Exception as exc:  # noqa: BLE001 - classify at the runtime boundary
            result["reason"] = _safe_category(exc, deadline=deadline, clock=self.clock)
            log.warning("acceleration verification component=embedding category=%s", result["reason"])
            return result
        for card in cards:
            row = self._verify_embedding_card(card, reference, deadline, expected_provider)
            result["cards"].append(row)
            result["active_providers"] = sorted(set(result["active_providers"]) | set(row.get("active_providers", [])))
            if row.get("delta") is not None:
                result["canary"] = {"state": "passed" if row["state"] == "passed" else "failed", "delta": row["delta"]}
            if _remaining(deadline, self.clock) <= 0:
                break
        if any(row["state"] != "passed" for row in result["cards"]):
            result["reason"] = next((row["reason"] for row in result["cards"] if row["state"] != "passed"), "canary_failed")
        return result

    def _verify_embedding_card(self, card: GpuDevice, reference: list[float] | None, deadline: float, expected_provider: str) -> dict[str, Any]:
        row: dict[str, Any] = {"component": "embedding", "pci_address": card.pci_address, "state": "failed", "reason": "runtime_missing", "active_providers": [], "delta": None, "cleanup": "pending"}
        remaining = _remaining(deadline, self.clock)
        if remaining <= 0:
            row["reason"] = "verification_timeout"
            row["cleanup"] = "not_started"
            return row
        worker = None
        try:
            log.info("acceleration verification worker_start component=embedding pci=%s", card.pci_address)
            worker = self.worker_factory(card, self._worker_config(remaining), self.probe)
            worker.start()
            row["active_providers"] = [value for value in str(getattr(worker.stats, "provider_active", "")).split(",") if value]
            if expected_provider not in row["active_providers"]:
                row["reason"] = "provider_cpu_fallback"
                log.warning(
                    "acceleration verification component=embedding pci=%s category=provider_cpu_fallback expected_provider=%s active=%s",
                    card.pci_address, expected_provider, ",".join(row["active_providers"]) or "none",
                )
                return row
            if _remaining(deadline, self.clock) <= 0:
                row["reason"] = "verification_timeout"
                return row
            candidate = worker.embed([CANARY_TEXT], timeout=_remaining(deadline, self.clock), record=False)[0]
            if _remaining(deadline, self.clock) <= 0:
                row["reason"] = "verification_timeout"
                return row
            dimensions = int(getattr(worker.config, "embedding_dimensions", 0) or 0)
            if not candidate or (dimensions and len(candidate) != dimensions):
                row["reason"] = "canary_failed"
                return row
            if not all(math.isfinite(float(value)) for value in candidate):
                row["reason"] = "canary_failed"
                return row
            if reference is not None:
                if len(candidate) != len(reference):
                    row["reason"] = "canary_failed"
                    return row
                row["delta"] = max(abs(float(a) - float(b)) for a, b in zip(reference, candidate, strict=True))
                if row["delta"] > float(getattr(self.config, "gpu_canary_tolerance", 1e-4)):
                    row["reason"] = "canary_failed"
                    return row
            else:
                # Hardware-free verifier tests can omit the service reference;
                # production wiring always supplies it after engine startup.
                row["delta"] = None
            row["state"] = "passed"
            row["reason"] = None
            log.info("acceleration verification canary component=embedding pci=%s state=passed", card.pci_address)
            return row
        except Exception as exc:  # noqa: BLE001 - worker boundary is classified
            row["reason"] = _safe_category(exc, deadline=deadline, clock=self.clock)
            log.warning(
                "acceleration verification canary component=embedding pci=%s category=%s",
                card.pci_address,
                row["reason"],
            )
            return row
        finally:
            if worker is not None:
                try:
                    worker.terminate(grace=min(2.0, max(0.05, _remaining(deadline, self.clock))))
                    row["cleanup"] = "passed" if getattr(worker, "proc", None) is None else "failed"
                except Exception:  # pragma: no cover - defensive teardown boundary
                    row["cleanup"] = "failed"
                    row["reason"] = row["reason"] or "worker_cleanup_failed"
                if row["cleanup"] != "passed":
                    row["state"] = "failed"
                    row["reason"] = row["reason"] or "worker_cleanup_failed"
                    log.error("acceleration verification component=embedding category=worker_cleanup_failed pci=%s", card.pci_address)

    def _verify_ocr(self, cards: list[GpuDevice], deadline: float) -> dict[str, Any]:
        result: dict[str, Any] = {"ready": False, "qualification": "failed", "cards": []}
        if not cards:
            result["qualification"] = "not_run"
            result["reason"] = "configured_card_missing"
            return result
        for card in cards:
            row = self._verify_ocr_card(card, deadline)
            result["cards"].append(row)
            if _remaining(deadline, self.clock) <= 0:
                break
        if any(row["state"] != "passed" for row in result["cards"]):
            result["reason"] = next((row["reason"] for row in result["cards"] if row["state"] != "passed"), "model_unqualified")
        else:
            result["qualification"] = "passed"
        return result

    def _verify_ocr_card(self, card: GpuDevice, deadline: float) -> dict[str, Any]:
        row: dict[str, Any] = {"component": "ocr", "pci_address": card.pci_address, "state": "failed", "reason": "stable_uuid_missing" if not card.unique_id else "model_unqualified", "cleanup": "pending"}
        if not card.unique_id:
            row["cleanup"] = "not_started"
            return row
        remaining = _remaining(deadline, self.clock)
        if remaining <= 0:
            row["reason"] = "verification_timeout"
            row["cleanup"] = "not_started"
            return row
        config = self.config
        python = getattr(config, "ocr_python", "/opt/cognita-runtimes/ocr/bin/python")
        model_dir = getattr(config, "ocr_model_dir", "/var/lib/cognita/models/easyocr")
        runner = self.ocr_factory(config, python=python, model_dir=model_dir, device="gpu", device_id=f"GPU-{card.unique_id}")
        try:
            log.info("acceleration verification worker_start component=ocr pci=%s", card.pci_address)
            payload = asyncio.run(runner.run(_probe_png(), ("en",), timeout_seconds=remaining, device="gpu", device_id=f"GPU-{card.unique_id}"))
            if _remaining(deadline, self.clock) <= 0:
                row["reason"] = "verification_timeout"
                return row
            if payload.device != "gpu" or payload.device_binding != f"GPU-{card.unique_id}":
                row["reason"] = "canary_failed"
                return row
            row["state"] = "passed"
            row["reason"] = None
            row["model_fingerprint"] = payload.model_fingerprint
            log.info("acceleration verification canary component=ocr pci=%s state=passed", card.pci_address)
        except Exception as exc:  # noqa: BLE001 - OCR worker boundary is classified
            row["reason"] = _safe_category(exc, deadline=deadline, clock=self.clock)
            log.warning(
                "acceleration verification canary component=ocr pci=%s category=%s",
                card.pci_address,
                row["reason"],
            )
        finally:
            # OCRWorkerRunner owns and reaps its process in run(); this marker
            # is deliberately explicit for diagnostics and hardware-free fakes.
            row["cleanup"] = "passed"
        return row


class FakeAccelerationVerifier:
    """Small test helper that records calls without importing GPU runtimes."""

    def __init__(self, result: dict[str, Any] | None = None):
        self.result = result or {"embedding": {"ready": False, "cards": []}, "ocr": {"ready": False, "cards": []}, "cards": {}, "cleanup": "passed"}
        self.calls: list[tuple[Any, list[GpuDevice], str, float]] = []

    def verify(self, *, policy: Any, cards: list[GpuDevice], profile: str, deadline: float) -> dict[str, Any]:
        self.calls.append((copy.deepcopy(policy), list(cards), profile, deadline))
        return copy.deepcopy(self.result)
