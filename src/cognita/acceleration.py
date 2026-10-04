"""Admin-owned acceleration policy and bounded runtime diagnostics.

The acceleration record is deliberately independent from ``cognita.yaml``.  The
service may read the old fields once during migration, but Admin writes only this
small, revisioned document.  Runtime facts are computed from the deployment
profile and the read-only GPU probe (AMD sysfs or NVIDIA NVML, chosen by the
profile); they are never accepted from the browser.

15.0 (DESIGN-NVIDIA-ACCELERATION §3, §7, §8): every `profile == "amd"` literal
is a lookup in `acceleration_profiles`; the card record key `rocm_uuid` is now
`gpu_uuid` (the value is `GPU-<unique_id>` for both vendors); and the UUID
pattern accepts NVIDIA's dashed form as well as AMD's plain hex.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acceleration_profiles import current_profile, gpu_settings_profile, profile_named
from .gpu_probe import GpuDevice, batch_ceiling_gb, default_probe, gate_devices
from .acceleration_verify import (
    VERIFY_DEADLINE_SECONDS,
    AccelerationVerifier,
    RealAccelerationVerifier,
)

log = logging.getLogger("cognita.acceleration")

_PCI = re.compile(r"^(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", re.I)
# AMD's `GPU-<hex>` and NVIDIA's `GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630`
# (NVML returns the dashed form; measured on the 4090, DESIGN-NVIDIA §1.5).
# The old pattern `^GPU-[0-9a-f]+$` refused every NVIDIA card for OCR.
_UUID = re.compile(r"^GPU-[0-9a-f]+(-[0-9a-f]+)*$", re.I)
LEGACY_ENV_KEYS = frozenset({
    "COGNITA_GPU_ENABLED", "COGNITA_GPU_CARDS", "COGNITA_GPU_VENV_PYTHON",
    "COGNITA_OCR_DEVICE", "COGNITA_OCR_GPU_DEVICE_ID", "COGNITA_OCR_GPU_DEVICE_IDS",
    "COGNITA_OCR_PYTHON", "COGNITA_OCR_LAUNCHER",
})
# The ORT provider this process expects, from the current acceleration profile
# (a `cpu` profile resolves GPU settings as `amd`, exactly as before 15.0).
# `AccelerationStore.status(profile=...)` computes the same value from the
# profile it is handed, so the status is right for the profile asked about
# even when the environment names another.
EMBED_PROVIDER = gpu_settings_profile(current_profile()).embed_provider

# The bounded reason enum (DESIGN-12.4.0 §5, plus `driver_too_old` from
# DESIGN-NVIDIA-ACCELERATION §7). Behavior and tests key on these tokens; the
# Admin page turns the ones a person must act on into plain words
# (`web/app.js`, `GPU_REASON_TEXT`).
FALLBACK_REASONS = frozenset({
    "profile_cpu", "device_nodes_missing", "device_permission_denied",
    "runtime_missing", "runtime_integrity_failed", "provider_unavailable",
    "provider_cpu_fallback", "canary_failed", "model_unqualified",
    "not_selected", "configured_card_missing", "stable_uuid_missing",
    "insufficient_vram", "busy", "telemetry_unavailable", "cooldown",
    "quarantined", "configuration_invalid", "restart_required",
    "verification_timeout", "worker_cleanup_failed",
    # NVIDIA: the driver is older than the CUDA 13 floor (R580).
    "driver_too_old",
    # 15.0.3: OCR is admitted to a card through the index scheduler's handler
    # for it, and the service builds those only while Knowledge GPU is on
    # (`__main__._build_engine_host`), so OCR set to GPU alone runs on the CPU.
    "knowledge_gpu_off",
})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_unique_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_UniqueKeyLoader)


def _pci(value: str) -> str:
    value = str(value).strip().lower()
    if not _PCI.fullmatch(value):
        raise ValueError("gpu_device_ids must contain canonical PCI addresses")
    if value.count(":") == 1:
        value = "0000:" + value
    return value


def _uuid(value: str) -> str:
    value = str(value).strip()
    if not _UUID.fullmatch(value):
        raise ValueError("ocr gpu_device_ids must contain GPU-<UUID> values (AMD hex or NVIDIA dashed)")
    return "GPU-" + value[4:].lower()


class KnowledgeAcceleration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gpu_enabled: bool = False
    gpu_device_ids: list[str] = Field(default_factory=list)

    @field_validator("gpu_device_ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        result = [_pci(value) for value in values]
        if len(set(result)) != len(result):
            raise ValueError("gpu_device_ids must contain unique PCI addresses")
        return result


class OcrAcceleration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = "cpu"
    gpu_device_ids: list[str] = Field(default_factory=list)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        if value not in {"cpu", "gpu"}:
            raise ValueError("ocr device must be cpu or gpu")
        return value

    @field_validator("gpu_device_ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        result = [_uuid(value) for value in values]
        if len(set(result)) != len(result):
            raise ValueError("ocr gpu_device_ids must contain unique UUIDs")
        return result

    @model_validator(mode="after")
    def cpu_has_no_explicit_selection(self) -> "OcrAcceleration":
        if self.device == "cpu" and self.gpu_device_ids:
            raise ValueError("CPU OCR cannot specify GPU card selection")
        return self


class AccelerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_version: int = Field(default=1, alias="schema")
    revision: int = 0
    knowledge: KnowledgeAcceleration = Field(default_factory=KnowledgeAcceleration)
    ocr: OcrAcceleration = Field(default_factory=OcrAcceleration)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported acceleration schema")
        return value

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("revision must be a nonnegative integer")
        return value

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def default_policy() -> AccelerationPolicy:
    return AccelerationPolicy()


class AccelerationConflict(RuntimeError):
    def __init__(self, current: AccelerationPolicy):
        super().__init__("acceleration policy revision is stale")
        self.current = current


class AccelerationConfigurationError(RuntimeError):
    pass


class AccelerationStore:
    """Owner-only atomic store with optimistic concurrency and replay safety."""

    def __init__(
        self,
        path: Path,
        *,
        legacy_config_path: Path | None = None,
        probe=None,
        runtime_config: Any | None = None,
        verifier: AccelerationVerifier | None = None,
        clock=time.monotonic,
    ):
        self.path = Path(path)
        self.previous_path = self.path.with_name(self.path.name + ".previous")
        self.legacy_config_path = Path(legacy_config_path) if legacy_config_path else None
        self.probe = probe or default_probe()
        self.runtime_config = runtime_config
        self._clock = clock
        self._verifier = verifier or RealAccelerationVerifier(runtime_config, self.probe, clock=clock)
        self._lock = threading.RLock()
        self._verify_lock = threading.Lock()
        # The token is bound to the complete mutation request. A replay returns
        # the policy produced by that request even after later successful
        # updates; reusing a token for different content fails closed.
        self._idempotency: dict[str, tuple[str, dict[str, Any]]] = {}
        self._configured: AccelerationPolicy | None = None
        self._loaded: AccelerationPolicy | None = None
        self._migration_warnings: list[str] = []
        self._last_summary: tuple[Any, ...] | None = None
        self._configuration_error: str | None = None
        # The index scheduler's own view of each card (`set_scheduler_cards`),
        # or None when no engine is attached (tests, a CPU-only host).
        self._scheduler_cards: Callable[[], list[dict[str, Any]]] | None = None
        self._verification: dict[str, Any] = {
            "state": "not_run",
            "observed_at": None,
            "result": None,
        }
        try:
            self._load_or_migrate()
        except AccelerationConfigurationError as exc:
            # Knowledge/Admin remain available on CPU so an operator can see
            # the diagnosis and perform the stopped-service previous-copy
            # repair.  Never substitute the previous bytes automatically.
            self._configuration_error = str(exc)
            self._configured = default_policy()
            self._loaded = default_policy()
            log.error("acceleration configuration invalid reason=configuration_invalid")

    @property
    def configured(self) -> AccelerationPolicy:
        with self._lock:
            return copy.deepcopy(self._configured or default_policy())

    @property
    def loaded(self) -> AccelerationPolicy:
        with self._lock:
            return copy.deepcopy(self._loaded or self.configured)

    @property
    def migration_warnings(self) -> list[str]:
        return list(self._migration_warnings)

    def set_cpu_embedder(self, embedder: Any) -> None:
        """Give live verification the engine's existing CPU canary primitive."""
        setter = getattr(self._verifier, "set_cpu_embedder", None)
        if setter is not None:
            setter(embedder)

    def set_scheduler_cards(self, reader: Callable[[], list[dict[str, Any]]]) -> None:
        """Let the status read what indexing is actually doing with each card.

        15.0.3: the scheduler quarantines a card that fails its own startup
        check (a canary, a provider, an old driver) for the life of the
        process, and indexing then runs on the CPU.  Verify cannot see that;
        without this the page would report the card in use.
        """
        self._scheduler_cards = reader

    def load_revision(self, *, loaded: bool = True) -> AccelerationPolicy:
        with self._lock:
            self._configuration_error = None
            old_loaded = self._loaded.revision if self._loaded is not None else None
            policy = self._read() if self.path.exists() else self.configured
            self._configured = policy
            if loaded:
                self._loaded = copy.deepcopy(policy)
                if old_loaded != policy.revision:
                    log.info("acceleration policy loaded revision=%d", policy.revision)
            return copy.deepcopy(policy)

    def rollback(self) -> AccelerationPolicy:
        """Restore the last valid primary, intended for stopped-service repair."""
        with self._lock:
            if not self.previous_path.is_file() or self.previous_path.is_symlink():
                raise AccelerationConfigurationError("no validated acceleration rollback copy exists")
            previous = AccelerationPolicy.model_validate(
                _load_unique_yaml(self.previous_path.read_text(encoding="utf-8"))
            )
            self._write_atomic(previous, preserve_previous=False)
            self._configured = previous
            self._loaded = copy.deepcopy(previous)
            self._configuration_error = None
            log.warning("acceleration policy rolled back revision=%d", previous.revision)
            return copy.deepcopy(previous)

    def _read(self) -> AccelerationPolicy:
        try:
            self._secure_existing(self.path)
            raw = _load_unique_yaml(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("acceleration.yaml must contain a mapping")
            return AccelerationPolicy.model_validate(raw)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
            raise AccelerationConfigurationError("acceleration configuration is invalid") from exc

    @staticmethod
    def _secure_existing(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("acceleration configuration must be a regular file")
        try:
            mode = path.stat().st_mode
            if os.name == "posix" and mode & 0o077:
                raise ValueError("acceleration configuration must be owner-only")
            if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                raise ValueError("acceleration configuration has unexpected ownership")
        except OSError as exc:
            raise ValueError("acceleration configuration cannot be inspected") from exc

    def _load_or_migrate(self) -> None:
        with self._lock:
            if self.path.exists():
                self._configured = self._read()
                self._loaded = copy.deepcopy(self._configured)
                return
            policy = self._migrate_legacy()
            self._write_atomic(policy, preserve_previous=False)
            self._configured = policy
            self._loaded = copy.deepcopy(policy)

    def _migrate_legacy(self) -> AccelerationPolicy:
        raw: dict[str, Any] = {}
        if self.legacy_config_path and self.legacy_config_path.is_file():
            try:
                value = yaml.safe_load(self.legacy_config_path.read_text(encoding="utf-8")) or {}
                raw = value if isinstance(value, dict) else {}
            except (OSError, UnicodeError, yaml.YAMLError):
                raw = {}
        enabled = bool(raw.get("gpu_enabled", False))
        selected: list[str] = []
        for value in raw.get("gpu_device_ids", []) or []:
            try:
                selected.append(_pci(value))
            except (TypeError, ValueError):
                self._migration_warnings.append("ignored_invalid_gpu_device_ids")
        ocr_device = raw.get("ocr_device", "cpu")
        if ocr_device != "gpu":
            ocr_device = "cpu"
        ocr_ids: list[str] = []
        legacy_ocr = raw.get("ocr_gpu_device_ids", []) or []
        if raw.get("ocr_gpu_device_id"):
            legacy_ocr = [raw["ocr_gpu_device_id"]]
        for value in legacy_ocr:
            try:
                ocr_ids.append(_uuid(value))
            except (TypeError, ValueError):
                self._migration_warnings.append("ignored_invalid_ocr_gpu_device_ids")
        for key in ("gpu_venv_python", "ocr_python", "ocr_launcher"):
            if raw.get(key):
                self._migration_warnings.append(f"ignored_host_runtime_{key}")
        if self._migration_warnings:
            log.warning("acceleration migration warnings=%s", self._migration_warnings)
        if ocr_device == "cpu":
            ocr_ids = []
        return AccelerationPolicy(
            revision=1,
            knowledge=KnowledgeAcceleration(gpu_enabled=enabled, gpu_device_ids=selected),
            ocr=OcrAcceleration(device=ocr_device, gpu_device_ids=ocr_ids),
        )

    def _write_atomic(self, policy: AccelerationPolicy, *, preserve_previous: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._secure_existing(self.path)
        payload = yaml.safe_dump(policy.public(), sort_keys=False).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if preserve_previous and self.path.exists():
                old = self.path.read_bytes()
                pfd, pname = tempfile.mkstemp(prefix=f".{self.previous_path.name}.", dir=self.path.parent)
                try:
                    os.fchmod(pfd, 0o600)
                    with os.fdopen(pfd, "wb") as previous:
                        previous.write(old)
                        previous.flush()
                        os.fsync(previous.fileno())
                    os.replace(pname, self.previous_path)
                finally:
                    if os.path.exists(pname):
                        os.unlink(pname)
            os.replace(name, self.path)
            try:
                dfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def update(self, expected_revision: int, candidate: dict[str, Any], token: str) -> AccelerationPolicy:
        with self._lock:
            if self._configuration_error:
                raise AccelerationConfigurationError("acceleration configuration is invalid")
            request_digest = hashlib.sha256(
                json.dumps(
                    {"expected_revision": expected_revision, "candidate": candidate},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if token and token in self._idempotency:
                prior_digest, prior_policy = self._idempotency[token]
                if prior_digest != request_digest:
                    raise ValueError("idempotency_token was reused with different content")
                return AccelerationPolicy.model_validate(copy.deepcopy(prior_policy))
            current = self._configured or default_policy()
            if expected_revision != current.revision:
                raise AccelerationConflict(copy.deepcopy(current))
            if not isinstance(candidate, dict):
                raise ValueError("request body must be a JSON object")
            unknown = set(candidate) - {"knowledge", "ocr"}
            if unknown:
                raise ValueError("unknown acceleration field(s): " + ", ".join(sorted(unknown)))
            if set(candidate) != {"knowledge", "ocr"}:
                raise ValueError("knowledge and ocr are required")
            try:
                available = self.probe.devices()
            except Exception:
                available = []
            current_pci = {card.pci_address for card in available}
            current_uuid = {f"GPU-{card.unique_id}" for card in available if card.unique_id}
            proposed_k = KnowledgeAcceleration.model_validate(candidate["knowledge"])
            proposed_o = OcrAcceleration.model_validate(candidate["ocr"])
            old_k = set(current.knowledge.gpu_device_ids)
            old_o = set(current.ocr.gpu_device_ids)
            new_k = set(proposed_k.gpu_device_ids) - old_k
            new_o = set(proposed_o.gpu_device_ids) - old_o
            if new_k - current_pci:
                raise ValueError("new Knowledge card selection must match a detected PCI address")
            if new_o - current_uuid:
                raise ValueError("new OCR card selection must match a detected GPU UUID")
            next_policy = AccelerationPolicy(
                revision=current.revision + 1,
                knowledge=proposed_k,
                ocr=proposed_o,
            )
            self._write_atomic(next_policy, preserve_previous=True)
            self._configured = next_policy
            log.info(
                "acceleration policy configured revision=%d loaded_revision=%d restart_required=true",
                next_policy.revision,
                self._loaded.revision if self._loaded is not None else -1,
            )
            if token:
                self._idempotency[token] = (request_digest, next_policy.public())
                if len(self._idempotency) > 256:
                    self._idempotency.pop(next(iter(self._idempotency)))
            return copy.deepcopy(next_policy)

    def _card_view(self, card: GpuDevice, policy: AccelerationPolicy, profile: str, runtime: dict[str, Any]) -> dict[str, Any]:
        k_selected = not policy.knowledge.gpu_device_ids or card.pci_address in policy.knowledge.gpu_device_ids
        # With no OCR list, the service's OCR tries the cards Knowledge is
        # pinned to (`SchedulerOCRCapacityGate.attempt_devices` resolves the
        # Knowledge list), so the page must too.
        if policy.ocr.gpu_device_ids:
            o_selected = card.unique_id is not None and f"GPU-{card.unique_id}" in policy.ocr.gpu_device_ids
        else:
            o_selected = k_selected
        reasons: list[str] = []
        # `gpu` is true for `amd` and `nvidia`; a `cpu` profile has no GPU
        # runtime, so no card is usable however the probe lists it.
        gpu_profile = profile_named(profile).gpu
        if not gpu_profile:
            reasons.append("profile_cpu")
        if not k_selected and policy.knowledge.gpu_enabled:
            reasons.append("not_selected")
        if policy.ocr.device == "gpu" and not o_selected:
            reasons.append("not_selected")
        if not card.unique_id and policy.ocr.device == "gpu" and o_selected:
            reasons.append("stable_uuid_missing")
        # OCR reaches a card only through the scheduler handler that exists
        # while Knowledge GPU is on (FALLBACK_REASONS, `knowledge_gpu_off`).
        ocr_has_handler = policy.knowledge.gpu_enabled
        if gpu_profile and policy.ocr.device == "gpu" and o_selected and not ocr_has_handler:
            reasons.append("knowledge_gpu_off")
        # What indexing itself found with this card.  Its evidence outranks
        # Verify's in both directions: a card it quarantined stays on the CPU
        # until restart whatever Verify said, and a card that passed its own
        # startup canary in this process (`has_run`) is used even if Verify
        # later timed out or skipped it while another program held the memory.
        # With a scheduler attached, a card it has no handler for (the probe
        # failed when the engine was built) is never used.
        scheduler_cards = runtime.get("scheduler_cards")
        scheduled = (scheduler_cards or {}).get(card.sysfs_name) or {}
        knowledge_wanted = policy.knowledge.gpu_enabled and k_selected
        quarantined = scheduled.get("state") == "quarantined"
        indexing_proved = bool(scheduled.get("has_run")) and not quarantined
        no_handler = scheduler_cards is not None and not scheduled
        if quarantined and knowledge_wanted:
            reasons.append("quarantined")
        if no_handler and gpu_profile and knowledge_wanted:
            reasons.append("telemetry_unavailable")
        evidence = runtime.get("cards", {}).get(card.pci_address, {})
        embedding_evidence = evidence.get("embedding") or {}
        ocr_evidence = evidence.get("ocr") or {}
        verified = bool(runtime.get("verified", True))
        if verified:
            embedding_passed = bool(runtime.get("embedding_ready")) and embedding_evidence.get("state") == "passed"
            ocr_passed = bool(runtime.get("ocr_ready")) and ocr_evidence.get("state") == "passed"
        else:
            # Not yet verified in this process: judged on the runtime in the
            # image and the same memory check Verify applies to cards the user
            # did not name (`RealAccelerationVerifier._split_by_vram`), so
            # the display card neither indexing nor OCR would use is not
            # shown ready.
            fits = card.pci_address in runtime.get("vram_fit", ())
            k_fits = bool(policy.knowledge.gpu_device_ids) or fits
            o_fits = bool(policy.ocr.gpu_device_ids) or fits
            embedding_passed = bool(runtime.get("embedding_ready")) and k_fits
            ocr_passed = bool(runtime.get("ocr_ready")) and o_fits
            if not k_fits and knowledge_wanted and gpu_profile:
                reasons.append("insufficient_vram")
            if not o_fits and policy.ocr.device == "gpu" and o_selected and gpu_profile:
                reasons.append("insufficient_vram")
        if embedding_evidence.get("reason") and knowledge_wanted and not indexing_proved:
            reasons.append(str(embedding_evidence["reason"]))
        if ocr_evidence.get("reason") and policy.ocr.device == "gpu" and o_selected:
            reasons.append(str(ocr_evidence["reason"]))
        # Keep diagnostics stable for clients while avoiding duplicate reasons
        # when the same provider failure is reported at card and aggregate
        # levels.
        reasons = list(dict.fromkeys(reasons))
        result = {
            "pci_address": card.pci_address,
            # 15.0: was `rocm_uuid`. `GPU-<unique_id>` is the AMD ROCm UUID and
            # the NVIDIA CUDA UUID alike, so the key no longer names a vendor.
            "gpu_uuid": f"GPU-{card.unique_id}" if card.unique_id else None,
            "sysfs_name": card.sysfs_name,
            "name": card.name,
            "vram_total_bytes": card.vram_total,
            "vram_free_bytes": card.vram_free,
            "busy_percent": card.busy_percent,
            "observed_at": time.time(),
            "knowledge_selected": bool(k_selected),
            "ocr_selected": bool(o_selected),
            "knowledge_usable": bool(gpu_profile and k_selected and not quarantined and not no_handler
                                     and (indexing_proved or embedding_passed)),
            "ocr_usable": bool(gpu_profile and ocr_passed and o_selected and bool(card.unique_id)
                               and ocr_has_handler and not no_handler),
            # The scheduler's state for this card ("cold", "ready",
            # "quarantined", ...) and its reason in its own words, or None
            # when no engine is attached or indexing does not own the card.
            "indexing_state": scheduled.get("state"),
            "indexing_reason": scheduled.get("reason"),
            "current_eligibility": "eligible" if not reasons else "ineligible",
            "reasons": reasons,
        }
        return result

    def status(self, *, profile: str = "cpu", image_variant: str | None = None) -> dict[str, Any]:
        # An unknown name resolves to `cpu` (one warning line from
        # `profile_named`), as the old `in {"cpu", "amd"}` test did silently.
        prof = profile_named(profile)
        profile = prof.name
        # What GPU settings resolve to: the profile's own row when it has a GPU,
        # else the `amd` row (`gpu_settings_profile`), so a `cpu` deployment
        # reports the same expected provider it always did.
        settings = gpu_settings_profile(prof)
        expected_provider = settings.embed_provider
        configured = self.configured
        loaded = self.loaded
        runtime_root = (prof.runtime_root or "").rstrip("/")
        embedding_runtime_present = prof.gpu and Path(f"{runtime_root}/embed/bin/python").is_file()
        ocr_runtime_present = prof.gpu and Path(f"{runtime_root}/ocr/bin/python").is_file()
        # Runtime files and provider listings are packaging evidence; a live
        # canary establishes verification. Before Verify runs, show GPU use
        # from loaded policy and the service's scheduler/OCR observations
        # without claiming a check ran. Use Verify's result once available.
        verification = self._verification.get("result") or {}
        embedding_result = verification.get("embedding") or {}
        ocr_result = verification.get("ocr") or {}
        verified = self._verification.get("state", "not_run") != "not_run"
        # None means "no scheduler to ask" (or it could not be read), which
        # claims nothing about any card; a dict is the scheduler's full list.
        scheduler_cards: dict[str, dict[str, Any]] | None = None
        if self._scheduler_cards is not None:
            try:
                scheduler_cards = {str(row.get("device")): row for row in self._scheduler_cards()}
            except Exception:
                log.warning("acceleration status could not read the index scheduler's cards", exc_info=True)
        cards: list[GpuDevice] = []
        try:
            cards = self.probe.devices()
        except Exception:
            log.warning("GPU discovery failed", exc_info=True)
        # Verify's own memory check for cards the user did not name: the
        # model's ceiling only, no reserve and no busy limit (see
        # `RealAccelerationVerifier._split_by_vram` for why).
        batch = int(getattr(self.runtime_config, "gpu_batch_size", 4) or 4)
        vram_fit = {
            result.device.pci_address
            for result in gate_devices(
                list(cards), batch_ceiling_gb=batch_ceiling_gb(batch, settings),
                reserve_vram_gb=0.0, max_busy_percent=100,
            )
            if result.qualifies
        } if cards else set()
        runtime = {
            "verified": verified,
            "scheduler_cards": scheduler_cards,
            "vram_fit": vram_fit,
            "embedding_ready": (bool(embedding_result.get("ready")) if verified
                                else bool(embedding_runtime_present)),
            "ocr_ready": (bool(ocr_result.get("ready")) if verified
                          else bool(ocr_runtime_present)),
            "embedding_runtime_present": embedding_runtime_present,
            "ocr_runtime_present": ocr_runtime_present,
            "expected_provider": expected_provider,
            "active_providers": list(embedding_result.get("active_providers") or []),
            "canary": embedding_result.get("canary") or {"state": "not_run", "delta": None},
            "ocr_qualification": ocr_result.get("qualification", "not_run"),
            "cards": verification.get("cards") or {},
        }
        rows =[self._card_view(card, loaded, profile, runtime) for card in cards]
        reasons: list[str] = ["configuration_invalid"] if self._configuration_error else []
        if not prof.gpu:
            reasons.append("profile_cpu")
        if configured.revision != loaded.revision:
            reasons.append("restart_required")
        detected_pci = {row["pci_address"] for row in rows}
        detected_ocr = {row["gpu_uuid"] for row in rows if row.get("gpu_uuid")}
        if loaded.knowledge.gpu_enabled and (not rows or any(item not in detected_pci for item in loaded.knowledge.gpu_device_ids)):
            reasons.append("configured_card_missing")
        if loaded.ocr.device == "gpu" and ((prof.gpu and not rows) or any(item not in detected_ocr for item in loaded.ocr.gpu_device_ids)):
            reasons.append("configured_card_missing")
        # Each card row already carries the runtime and evidence it needs (a
        # card indexing has proved counts even when Verify's result did not).
        knowledge_effective = loaded.knowledge.gpu_enabled and prof.gpu and any(row["knowledge_usable"] for row in rows)
        ocr_effective = loaded.ocr.device == "gpu" and prof.gpu and any(row["ocr_usable"] for row in rows)
        verification_reasons = [
            str(value.get("reason"))
            for value in (embedding_result, ocr_result)
            if value.get("reason")
        ]
        # Lift the card reasons that explain a whole-service fallback.
        card_reasons = {reason for row in rows for reason in row["reasons"]}
        observed = ("quarantined", "telemetry_unavailable", "insufficient_vram")
        if loaded.knowledge.gpu_enabled and not knowledge_effective:
            reasons.extend(reason for reason in observed if reason in card_reasons)
            # Verify passed, yet no card is in use: say what was observed
            # rather than blaming the runtime Verify just exercised.
            explained = {*observed, "configured_card_missing", "profile_cpu"} & set(reasons)
            if verified:
                if verification_reasons:
                    reasons.append(verification_reasons[0])
                elif not explained:
                    reasons.append("provider_cpu_fallback" if runtime["active_providers"] == ["CPUExecutionProvider"] else "runtime_missing")
            elif prof.gpu and not embedding_runtime_present:
                reasons.append("runtime_missing")
        if loaded.ocr.device == "gpu" and not ocr_effective:
            knowledge_off = prof.gpu and not loaded.knowledge.gpu_enabled
            if knowledge_off:
                reasons.append("knowledge_gpu_off")
            reasons.extend(reason for reason in ("stable_uuid_missing", "telemetry_unavailable", "insufficient_vram")
                           if reason in card_reasons)
            explained = {"knowledge_gpu_off", "stable_uuid_missing", "telemetry_unavailable", "insufficient_vram",
                         "configured_card_missing", "profile_cpu"} & set(reasons)
            if verified:
                verified_reason = next((value for value in verification_reasons if value != "profile_cpu"), None)
                if verified_reason:
                    reasons.append(verified_reason)
                elif not explained:
                    reasons.append("runtime_missing" if not runtime["ocr_runtime_present"] else "model_unqualified")
            elif prof.gpu and not ocr_runtime_present:
                reasons.append("runtime_missing")
        reasons = list(dict.fromkeys(reasons))
        result = {
            "schema": 1,
            "deployment": {"profile": profile, "image_variant": image_variant or profile, "devices_exposed": prof.devices_exposed, "vendor_label": prof.vendor_label, "restart_command_kind": "compose_service_restart"},
            "configured": configured.public(),
            "loaded": loaded.public(),
            "effective": {"knowledge_gpu": bool(knowledge_effective), "ocr_device": "gpu" if ocr_effective else "cpu", "fallback_reasons": reasons},
            "runtimes": {"embedding": {"expected_provider": expected_provider, "active_providers": runtime["active_providers"], "runtime_present": embedding_runtime_present, "ready": runtime["embedding_ready"], "canary": runtime["canary"]}, "ocr": {"runtime_present": ocr_runtime_present, "ready": runtime["ocr_ready"], "qualification": runtime["ocr_qualification"]}},
            "cards": rows,
            "restart": {"required": configured.revision != loaded.revision, "reason": "configured_revision_not_loaded" if configured.revision != loaded.revision else None},
            "verification": {"state": self._verification.get("state", "not_run"), "observed_at": self._verification.get("observed_at")},
            "migration_warnings": self.migration_warnings,
        }
        summary = (profile, configured.revision, loaded.revision, bool(knowledge_effective), "gpu" if ocr_effective else "cpu", tuple(reasons))
        if summary != self._last_summary:
            self._last_summary = summary
            log.info(
                "acceleration state profile=%s expected_provider=%s configured_revision=%d loaded_revision=%d knowledge_gpu=%s ocr_device=%s fallback_reasons=%s",
                profile, expected_provider, configured.revision, loaded.revision, bool(knowledge_effective),
                "gpu" if ocr_effective else "cpu", ",".join(reasons) or "none",
            )
        return result

    def verify(self, *, profile: str = "cpu", deadline_seconds: float = VERIFY_DEADLINE_SECONDS) -> dict[str, Any]:
        if not self._verify_lock.acquire(blocking=False):
            raise RuntimeError("verification_busy")
        try:
            started = self._clock()
            requested_deadline = float(deadline_seconds)
            if not math.isfinite(requested_deadline):
                requested_deadline = VERIFY_DEADLINE_SECONDS
            deadline = started + max(0.1, min(requested_deadline, VERIFY_DEADLINE_SECONDS))
            policy = self.loaded
            try:
                cards = self.probe.devices()
            except Exception:
                cards = []
            prof = profile_named(profile)
            result = self._verifier.verify(policy=policy, cards=cards, profile=prof.name, deadline=deadline)
            embedding = result.get("embedding") or {}
            ocr = result.get("ocr") or {}
            requested = []
            if policy.knowledge.gpu_enabled:
                requested.append(bool(embedding.get("ready")))
            if policy.ocr.device == "gpu":
                requested.append(bool(ocr.get("ready")))
            if not requested or not prof.gpu:
                state = "fallback"
            elif all(requested):
                state = "passed"
            else:
                reasons = [
                    value.get("reason")
                    for value in (embedding, ocr)
                    if value.get("reason")
                ]
                card_reasons = [
                    row.get("reason")
                    for row in embedding.get("cards", []) + ocr.get("cards", [])
                    if row.get("reason")
                ]
                state = "timeout" if "verification_timeout" in reasons + card_reasons else "failed"
            observed_at = time.time()
            self._verification = {"state": state, "observed_at": observed_at, "result": copy.deepcopy(result)}
            status = self.status(profile=prof.name)
            log.info(
                "acceleration verification state=%s profile=%s cards=%d cleanup=%s embedding_reason=%s ocr_reason=%s",
                state,
                prof.name,
                len(cards),
                result.get("cleanup", "unknown"),
                embedding.get("reason") or "none",
                ocr.get("reason") or "none",
            )
            duration = round(self._clock() - started, 3)
            return {"state": state, "observed_at": observed_at, "duration_seconds": duration, "deadline_seconds": round(deadline - started, 3), "cards": [{"pci_address": row["pci_address"], "component": row.get("component"), "state": row.get("state", "failed"), "reason": row.get("reason"), "cleanup": row.get("cleanup")} for row in (embedding.get("cards", []) + ocr.get("cards", []))], "runtimes": {"embedding": {"active_providers": embedding.get("active_providers", []), "canary": embedding.get("canary", {"state": "not_run", "delta": None}), "ready": bool(embedding.get("ready")), "reason": embedding.get("reason")}, "ocr": {"qualification": ocr.get("qualification", "not_run"), "ready": bool(ocr.get("ready")), "reason": ocr.get("reason")}}, "effective": status["effective"], "cleanup": result.get("cleanup", "unknown")}
        finally:
            self._verify_lock.release()


def acceleration_path(config: Any) -> Path:
    configured = getattr(config, "acceleration_path", None)
    if configured:
        return Path(configured)
    # ``load_config`` binds this to the mounted config directory.  Directly
    # constructed test/app configs use their data root so no policy file is
    # created in the source checkout as a side effect.
    return Path(getattr(config, "data_root", Path("data"))) / "acceleration.yaml"


def legacy_env_overrides(values: dict[str, str] | None = None) -> list[str]:
    env = os.environ if values is None else values
    return sorted(key for key in LEGACY_ENV_KEYS if key in env and str(env[key]).strip())


def preflight_legacy_env(values: dict[str, str] | None = None) -> None:
    stale = legacy_env_overrides(values)
    if stale:
        raise RuntimeError("legacy GPU/OCR environment overrides are rejected; remove " + ", ".join(stale) + " and use Admin Settings -> GPU acceleration")
