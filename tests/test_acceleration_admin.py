"""Focused contracts for the Admin-owned acceleration record."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cognita.acceleration import (
    AccelerationConflict,
    AccelerationStore,
    preflight_legacy_env,
)
from cognita.acceleration_verify import FakeAccelerationVerifier, RealAccelerationVerifier
from cognita.gpu_probe import GpuDevice


class Probe:
    def devices(self):
        return [
            GpuDevice(
                sysfs_name="card1", pci_address="0000:03:00.0", unique_id="0123abcd",
                name="AMD test", vram_total=32 * 1024**3, vram_free=30 * 1024**3,
                busy_percent=0,
            )
        ]


def test_store_migrates_legacy_policy_and_keeps_loaded_snapshot(tmp_path: Path):
    legacy = tmp_path / "cognita.yaml"
    legacy.write_text("gpu_enabled: true\ngpu_device_ids: ['0000:03:00.0']\nocr_device: gpu\nocr_gpu_device_ids: ['GPU-0123ABCD']\n", encoding="utf-8")
    store = AccelerationStore(tmp_path / "acceleration.yaml", legacy_config_path=legacy, probe=Probe())
    assert store.configured.revision == 1
    assert store.configured.knowledge.gpu_device_ids == ["0000:03:00.0"]
    assert store.configured.ocr.gpu_device_ids == ["GPU-0123abcd"]
    token = str(uuid4())
    candidate = {"knowledge": {"gpu_enabled": False, "gpu_device_ids": []}, "ocr": {"device": "cpu", "gpu_device_ids": []}}
    saved = store.update(1, candidate, token)
    assert saved.revision == 2
    # A later mutation must not change the original idempotent result.
    store.update(2, {"knowledge": {"gpu_enabled": True, "gpu_device_ids": []}, "ocr": {"device": "cpu", "gpu_device_ids": []}}, str(uuid4()))
    assert store.update(1, candidate, token).revision == 2
    with pytest.raises(ValueError, match="different content"):
        store.update(1, {"knowledge": {"gpu_enabled": True, "gpu_device_ids": []}, "ocr": {"device": "cpu", "gpu_device_ids": []}}, token)
    assert store.loaded.revision == 1
    assert store.status(profile="cpu")["restart"]["required"] is True


def test_store_rejects_stale_and_new_unknown_selection(tmp_path: Path):
    store = AccelerationStore(tmp_path / "acceleration.yaml", probe=Probe())
    with pytest.raises(ValueError, match="detected PCI"):
        store.update(1, {"knowledge": {"gpu_enabled": True, "gpu_device_ids": ["0000:04:00.0"]}, "ocr": {"device": "cpu", "gpu_device_ids": []}}, str(uuid4()))
    with pytest.raises(AccelerationConflict):
        store.update(0, {"knowledge": {"gpu_enabled": False, "gpu_device_ids": []}, "ocr": {"device": "cpu", "gpu_device_ids": []}}, str(uuid4()))


def test_store_rejects_duplicate_yaml_keys(tmp_path: Path):
    path = tmp_path / "acceleration.yaml"
    path.write_text(
        "schema: 1\nrevision: 1\nrevision: 2\nknowledge: {}\nocr: {}\n",
        encoding="utf-8",
    )
    store = AccelerationStore(path, probe=Probe())
    assert store.status()["effective"]["fallback_reasons"][0] == "configuration_invalid"


def test_legacy_environment_is_rejected_without_disclosing_values():
    with pytest.raises(RuntimeError, match="COGNITA_GPU_ENABLED") as error:
        preflight_legacy_env({"COGNITA_GPU_ENABLED": "/private/host/path"})
    assert "/private/host/path" not in str(error.value)


def _enabled_legacy(tmp_path: Path) -> Path:
    legacy = tmp_path / "cognita.yaml"
    legacy.write_text(
        "gpu_enabled: true\ngpu_device_ids: ['0000:03:00.0']\n"
        "ocr_device: gpu\nocr_gpu_device_ids: ['GPU-0123abcd']\n",
        encoding="utf-8",
    )
    return legacy


def _verification_result(
    *, reason: str | None = None, cleanup: str = "passed",
    pci: str = "0000:03:00.0", provider: str = "MIGraphXExecutionProvider",
) -> dict:
    effective_reason = reason or ("worker_cleanup_failed" if cleanup != "passed" else None)
    embedding_row = {
        "component": "embedding", "pci_address": pci,
        "state": "passed" if effective_reason is None else "failed", "reason": effective_reason,
        "active_providers": [provider], "cleanup": cleanup,
    }
    ocr_row = {
        "component": "ocr", "pci_address": pci,
        "state": "passed" if effective_reason is None else "failed", "reason": effective_reason,
        "cleanup": cleanup,
    }
    return {
        "embedding": {
            "ready": effective_reason is None, "active_providers": [provider],
            "canary": {"state": "passed" if effective_reason is None else "failed", "delta": None},
            "cards": [embedding_row], "reason": effective_reason,
        },
        "ocr": {
            "ready": effective_reason is None, "qualification": "passed" if effective_reason is None else "failed",
            "cards": [ocr_row], "reason": effective_reason,
        },
        "cards": {pci: {"embedding": embedding_row, "ocr": ocr_row}},
        "cleanup": cleanup,
    }


def test_verify_reports_live_per_card_success_and_effective_state(tmp_path: Path):
    verifier = FakeAccelerationVerifier(_verification_result())
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_enabled_legacy(tmp_path),
        probe=Probe(), verifier=verifier,
    )
    result = store.verify(profile="amd", deadline_seconds=5)
    assert result["state"] == "passed"
    assert result["cleanup"] == "passed"
    assert {row["component"] for row in result["cards"]} == {"embedding", "ocr"}
    assert result["effective"] == {"knowledge_gpu": True, "ocr_device": "gpu", "fallback_reasons": []}
    assert verifier.calls and verifier.calls[0][2] == "amd"
    assert verifier.calls[0][3] > 0


@pytest.mark.parametrize("reason,state", [("canary_failed", "failed"), ("verification_timeout", "timeout")])
def test_verify_preserves_safe_failure_category_and_never_mutates_policy(tmp_path: Path, reason: str, state: str):
    verifier = FakeAccelerationVerifier(_verification_result(reason=reason))
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_enabled_legacy(tmp_path),
        probe=Probe(), verifier=verifier,
    )
    before = store.loaded.public()
    result = store.verify(profile="amd", deadline_seconds=5)
    assert result["state"] == state
    assert reason in result["effective"]["fallback_reasons"]
    assert store.loaded.public() == before


def test_verify_reports_cleanup_failure_and_is_replayable(tmp_path: Path):
    verifier = FakeAccelerationVerifier(_verification_result(cleanup="failed"))
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_enabled_legacy(tmp_path),
        probe=Probe(), verifier=verifier,
    )
    result = store.verify(profile="amd")
    assert result["state"] == "failed"
    assert result["cleanup"] == "failed"
    assert "worker_cleanup_failed" in result["effective"]["fallback_reasons"]
    assert store.status(profile="amd")["verification"]["state"] == "failed"


class _FakeGpuWorker:
    def __init__(self, card, config, probe):
        self.config = config
        self.stats = SimpleNamespace(provider_active="MIGraphXExecutionProvider")
        self.proc = object()
        self.terminated = False

    def start(self):
        return None

    def embed(self, texts, *, timeout, record):
        return [[0.25] * self.config.embedding_dimensions]

    def terminate(self, *, grace):
        self.terminated = True
        self.proc = None


def test_real_verifier_uses_injected_worker_and_reaps_it_without_hardware():
    from cognita.acceleration import AccelerationPolicy, KnowledgeAcceleration, OcrAcceleration

    card = Probe().devices()[0]
    policy = AccelerationPolicy(
        revision=1,
        knowledge=KnowledgeAcceleration(gpu_enabled=True, gpu_device_ids=[card.pci_address]),
        ocr=OcrAcceleration(device="cpu"),
    )
    verifier = RealAccelerationVerifier(SimpleNamespace(embedding_dimensions=3), Probe(), worker_factory=_FakeGpuWorker)
    result = verifier.verify(policy=policy, cards=[card], profile="amd", deadline=10**9)
    assert result["embedding"]["ready"] is True
    assert result["embedding"]["cards"][0]["cleanup"] == "passed"
    assert result["cleanup"] == "passed"


# kei, 2026-09-29: two 32 GB cards and a display card with under 1 GB free.
_DISPLAY_CARD = GpuDevice(
    sysfs_name="card3", pci_address="0000:7e:00.0", unique_id=None,
    name="display", vram_total=2 * 1024**3, vram_free=int(0.98 * 1024**3), busy_percent=0,
)


def _all_cards_policy(*, knowledge_ids=(), ocr_device="gpu"):
    from cognita.acceleration import AccelerationPolicy, KnowledgeAcceleration, OcrAcceleration

    return AccelerationPolicy(
        revision=1,
        knowledge=KnowledgeAcceleration(gpu_enabled=True, gpu_device_ids=list(knowledge_ids)),
        ocr=OcrAcceleration(device=ocr_device),
    )


def _recording_factory(started: list[str]):
    def factory(card, config, probe):
        started.append(card.pci_address)
        return _FakeGpuWorker(card, config, probe)
    return factory


class _PassingOcr:
    def __init__(self, config, *, python, model_dir, device, device_id):
        self.device_id = device_id

    async def run(self, image, languages, *, timeout_seconds, device, device_id):
        return SimpleNamespace(device="gpu", device_binding=device_id, model_fingerprint="fp")


def test_all_cards_skips_a_card_too_small_for_the_model():
    big = Probe().devices()[0]
    started: list[str] = []
    verifier = RealAccelerationVerifier(
        SimpleNamespace(embedding_dimensions=3), Probe(),
        worker_factory=_recording_factory(started), ocr_factory=_PassingOcr,
    )
    result = verifier.verify(policy=_all_cards_policy(), cards=[big, _DISPLAY_CARD], profile="amd", deadline=10**9)
    assert started == [big.pci_address]
    assert result["embedding"]["ready"] is True
    assert result["ocr"]["ready"] is True
    assert result["cleanup"] == "passed"
    skipped = result["cards"][_DISPLAY_CARD.pci_address]
    assert skipped["embedding"]["state"] == "skipped"
    assert skipped["embedding"]["reason"] == "insufficient_vram"
    assert skipped["ocr"]["state"] == "skipped"


def test_a_busy_card_with_room_for_the_model_is_still_tried():
    # 5 GB free: less than model + the 4 GB indexing reserve, more than the model.  A failed check is
    # permanent (the CPU image is staged), so other apps' momentary load must not decide it.
    busy = GpuDevice(sysfs_name="card1", pci_address="0000:03:00.0", unique_id="0123abcd", name="AMD busy",
                     vram_total=32 * 1024**3, vram_free=5 * 1024**3, busy_percent=95)
    started: list[str] = []
    verifier = RealAccelerationVerifier(
        SimpleNamespace(embedding_dimensions=3), Probe(), worker_factory=_recording_factory(started),
    )
    result = verifier.verify(policy=_all_cards_policy(ocr_device="cpu"), cards=[busy, _DISPLAY_CARD],
                             profile="amd", deadline=10**9)
    assert started == [busy.pci_address]
    assert result["embedding"]["ready"] is True


def test_a_card_the_user_named_is_tried_even_when_small():
    started: list[str] = []
    verifier = RealAccelerationVerifier(
        SimpleNamespace(embedding_dimensions=3), Probe(), worker_factory=_recording_factory(started),
    )
    policy = _all_cards_policy(knowledge_ids=[_DISPLAY_CARD.pci_address], ocr_device="cpu")
    verifier.verify(policy=policy, cards=[_DISPLAY_CARD], profile="amd", deadline=10**9)
    assert started == [_DISPLAY_CARD.pci_address]


def test_no_card_with_room_is_not_ready_and_says_why():
    started: list[str] = []
    verifier = RealAccelerationVerifier(
        SimpleNamespace(embedding_dimensions=3), Probe(), worker_factory=_recording_factory(started),
    )
    result = verifier.verify(policy=_all_cards_policy(ocr_device="cpu"), cards=[_DISPLAY_CARD], profile="amd", deadline=10**9)
    assert started == []
    assert result["embedding"]["ready"] is False
    assert result["embedding"]["reason"] == "insufficient_vram"


# ------------------------------------------------------------------ 15.0: NVIDIA
# DESIGN-NVIDIA-ACCELERATION §3, §7, §8. Hardware-free: a fake NVML-shaped probe,
# fake workers and the fake verifier. No sleeps, no clocks, no polling.

_NVIDIA_UUID = "GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
_NVIDIA_PCI = "0000:01:00.0"


class NvidiaProbe:
    """What `NvmlProbe` reports for the 4090: dashed UUID with `GPU-` stripped."""

    def devices(self):
        return [
            GpuDevice(
                sysfs_name="gpu0", pci_address=_NVIDIA_PCI, unique_id=_NVIDIA_UUID[4:],
                name="NVIDIA GeForce RTX 4090", vram_total=24 * 1024**3,
                vram_free=22 * 1024**3, busy_percent=0,
            )
        ]


def _nvidia_legacy(tmp_path: Path) -> Path:
    legacy = tmp_path / "cognita.yaml"
    legacy.write_text(
        f"gpu_enabled: true\ngpu_device_ids: ['{_NVIDIA_PCI}']\n"
        f"ocr_device: gpu\nocr_gpu_device_ids: ['{_NVIDIA_UUID}']\n",
        encoding="utf-8",
    )
    return legacy


@pytest.mark.parametrize("given,normalized", [
    ("GPU-0123ABCD", "GPU-0123abcd"),                # AMD plain hex, as before
    (_NVIDIA_UUID, _NVIDIA_UUID),                    # NVIDIA dashed
    (_NVIDIA_UUID.upper(), _NVIDIA_UUID),            # case folds to lowercase
])
def test_ocr_uuid_accepts_amd_hex_and_nvidia_dashed_and_normalizes(given: str, normalized: str):
    from cognita.acceleration import OcrAcceleration

    policy = OcrAcceleration(device="gpu", gpu_device_ids=[given])
    assert policy.gpu_device_ids == [normalized]


@pytest.mark.parametrize("garbage", [
    "", "GPU-", "GPU-xyz", "GPU-abc-", "GPU--abc", "GPU-abc--def", "GPU-4cd2 8834",
    "GPU-abc_def", "card1", "0123abcd",
])
def test_ocr_uuid_refuses_garbage(garbage: str):
    from cognita.acceleration import OcrAcceleration

    with pytest.raises(ValueError, match="GPU-<UUID>"):
        OcrAcceleration(device="gpu", gpu_device_ids=[garbage])


def test_a_detected_nvidia_card_can_be_chosen_for_ocr_and_an_undetected_one_cannot(tmp_path: Path):
    store = AccelerationStore(tmp_path / "acceleration.yaml", probe=NvidiaProbe())
    saved = store.update(
        1,
        {"knowledge": {"gpu_enabled": False, "gpu_device_ids": []},
         "ocr": {"device": "gpu", "gpu_device_ids": [_NVIDIA_UUID]}},
        str(uuid4()),
    )
    assert saved.ocr.gpu_device_ids == [_NVIDIA_UUID]
    with pytest.raises(ValueError, match="detected GPU UUID"):
        store.update(
            2,
            {"knowledge": {"gpu_enabled": False, "gpu_device_ids": []},
             "ocr": {"device": "gpu",
                     "gpu_device_ids": ["GPU-00000000-0000-0000-0000-000000000000"]}},
            str(uuid4()),
        )


_RUNTIME_FILES = frozenset({"/opt/cognita-runtimes/embed/bin/python", "/opt/cognita-runtimes/ocr/bin/python"})


def _runtimes(monkeypatch, present=_RUNTIME_FILES):
    """Decide which GPU runtime interpreters exist, whatever this machine has.

    The release gate runs pytest in the `test` image, built from `app`, which
    really has `/opt/cognita-runtimes/ocr/bin/python` (the CPU OCR runtime).
    """
    real_is_file = Path.is_file

    def is_file(self):
        if self.as_posix() in _RUNTIME_FILES:
            return self.as_posix() in present
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)


def test_status_on_nvidia_reports_the_cuda_provider_and_exposed_devices(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch, present=frozenset())
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(),
    )
    status = store.status(profile="nvidia")
    assert status["deployment"]["profile"] == "nvidia"
    assert status["deployment"]["devices_exposed"] is True
    assert status["deployment"]["vendor_label"] == "NVIDIA"
    assert status["runtimes"]["embedding"]["expected_provider"] == "CUDAExecutionProvider"
    card = status["cards"][0]
    assert card["gpu_uuid"] == _NVIDIA_UUID
    # The old vendor-named key is gone from the record.
    assert "rocm_uuid" not in card
    # Neither GPU runtime is in this (simulated) image, so no card is usable.
    assert card["knowledge_usable"] is False and card["ocr_usable"] is False
    assert "profile_cpu" not in status["effective"]["fallback_reasons"]
    assert "runtime_missing" in status["effective"]["fallback_reasons"]


def test_before_verify_a_restarted_service_reports_the_card_it_is_using(tmp_path: Path, monkeypatch):
    """15.0.3: after every restart the page said "CPU fallback" (runtime_missing,
    model_unqualified) until someone pressed Verify, while indexing ran on the
    card; the service never waits for this page's check.  With the runtime in the
    image and the card selected, the state is the card, marked not yet verified."""
    _runtimes(monkeypatch)
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(),
    )
    status = store.status(profile="nvidia")
    assert status["verification"]["state"] == "not_run"
    assert status["effective"] == {"knowledge_gpu": True, "ocr_device": "gpu", "fallback_reasons": []}
    card = status["cards"][0]
    assert card["knowledge_usable"] is True and card["ocr_usable"] is True


def test_once_verify_has_failed_its_evidence_decides(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)
    result =_verification_result(reason="canary_failed", pci=_NVIDIA_PCI, provider="CUDAExecutionProvider")
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(), verifier=FakeAccelerationVerifier(result),
    )
    store.verify(profile="nvidia", deadline_seconds=5)
    status = store.status(profile="nvidia")
    assert status["effective"]["knowledge_gpu"] is False
    assert "canary_failed" in status["effective"]["fallback_reasons"]


def _passing_nvidia_verifier():
    return FakeAccelerationVerifier(_verification_result(pci=_NVIDIA_PCI, provider="CUDAExecutionProvider"))


@pytest.mark.parametrize("verify_first", [False, True])
def test_a_card_indexing_quarantined_is_not_reported_in_use(tmp_path: Path, monkeypatch, verify_first: bool):
    """A card that failed its own startup check (an old driver, a canary) is
    quarantined by the scheduler and indexing runs on the CPU.  Before or
    after Verify, the page must say so, not report the card in use."""
    _runtimes(monkeypatch)
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(), verifier=_passing_nvidia_verifier(),
    )
    store.set_scheduler_cards(lambda: [
        {"device": "gpu0", "state": "quarantined", "reason": "startup failed: canary_failed"},
    ])
    if verify_first:
        store.verify(profile="nvidia", deadline_seconds=5)
    status = store.status(profile="nvidia")
    assert status["effective"]["knowledge_gpu"] is False
    assert status["effective"]["fallback_reasons"] == ["quarantined"]
    card = status["cards"][0]
    assert card["knowledge_usable"] is False
    assert card["indexing_state"] == "quarantined"
    assert card["indexing_reason"] == "startup failed: canary_failed"
    # OCR runs in its own worker and is still admitted to the card.
    assert status["effective"]["ocr_device"] == "gpu"


def test_a_ready_card_in_the_scheduler_changes_nothing(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(),
    )
    store.set_scheduler_cards(lambda: [{"device": "gpu0", "state": "ready", "reason": None, "has_run": True}])
    status = store.status(profile="nvidia")
    assert status["effective"] == {"knowledge_gpu": True, "ocr_device": "gpu", "fallback_reasons": []}
    assert status["cards"][0]["indexing_state"] == "ready"


def test_a_scheduler_that_cannot_be_read_leaves_the_status_working(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(),
    )

    def broken():
        raise RuntimeError("scheduler gone")

    store.set_scheduler_cards(broken)
    status = store.status(profile="nvidia")
    assert status["effective"]["knowledge_gpu"] is True
    assert status["cards"][0]["indexing_state"] is None


@pytest.mark.parametrize("verify_first", [False, True])
def test_ocr_on_gpu_with_knowledge_gpu_off_runs_on_the_cpu_and_says_why(tmp_path: Path, monkeypatch, verify_first: bool):
    """The service admits OCR to a card through the scheduler's handler for
    it, and builds those only while Knowledge GPU is on."""
    _runtimes(monkeypatch)
    legacy = tmp_path / "cognita.yaml"
    legacy.write_text(
        f"gpu_enabled: false\nocr_device: gpu\nocr_gpu_device_ids: ['{_NVIDIA_UUID}']\n",
        encoding="utf-8",
    )
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=legacy,
        probe=NvidiaProbe(), verifier=_passing_nvidia_verifier(),
    )
    if verify_first:
        store.verify(profile="nvidia", deadline_seconds=5)
    status = store.status(profile="nvidia")
    assert status["effective"] == {"knowledge_gpu": False, "ocr_device": "cpu", "fallback_reasons": ["knowledge_gpu_off"]}
    card = status["cards"][0]
    assert card["ocr_usable"] is False
    assert "knowledge_gpu_off" in card["reasons"]


def test_before_verify_a_card_without_a_uuid_names_the_uuid_not_a_check_that_never_ran(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)

    class NoUuidProbe:
        def devices(self):
            return [GpuDevice(
                sysfs_name="gpu0", pci_address=_NVIDIA_PCI, unique_id=None,
                name="NVIDIA GeForce RTX 4090", vram_total=24 * 1024**3,
                vram_free=22 * 1024**3, busy_percent=0,
            )]

    legacy = tmp_path / "cognita.yaml"
    legacy.write_text(f"gpu_enabled: true\ngpu_device_ids: ['{_NVIDIA_PCI}']\nocr_device: gpu\n", encoding="utf-8")
    store = AccelerationStore(tmp_path / "acceleration.yaml", legacy_config_path=legacy, probe=NoUuidProbe())
    status = store.status(profile="nvidia")
    assert status["effective"] == {"knowledge_gpu": True, "ocr_device": "cpu", "fallback_reasons": ["stable_uuid_missing"]}


def test_before_verify_a_missing_card_does_not_blame_a_runtime_that_is_installed(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)
    # `_enabled_legacy` selects an AMD PCI address and UUID the NVIDIA probe does not report.
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_enabled_legacy(tmp_path), probe=NvidiaProbe(),
    )
    status = store.status(profile="nvidia")
    assert status["effective"] == {"knowledge_gpu": False, "ocr_device": "cpu", "fallback_reasons": ["configured_card_missing"]}


def test_the_scheduler_reports_each_cards_state_for_the_admin_page():
    from cognita.index_scheduler import DeviceHandler, IndexScheduler

    async def embed(texts):
        return texts

    card = DeviceHandler("gpu0", embed, kind="gpu", state="cold")
    scheduler = IndexScheduler(embed, gpu_devices=[card])
    assert scheduler.card_states() == [{"device": "gpu0", "state": "cold", "reason": None, "has_run": False}]
    card._was_ready = True
    card.state, card.reason = "quarantined", "startup failed: canary_failed"
    assert scheduler.card_states() == [
        {"device": "gpu0", "state": "quarantined", "reason": "startup failed: canary_failed", "has_run": True},
    ]


def test_the_engine_host_hands_its_scheduler_to_the_admin_status(tmp_path: Path, monkeypatch):
    """Without this one wiring line the page silently stops seeing quarantines."""
    import cognita.embeddings
    import cognita.engine_local
    import cognita.retrieval
    import cognita.store
    from cognita.__main__ import _build_engine_host
    from cognita.config import CognitaConfig
    from cognita.index_scheduler import IndexScheduler
    from cognita.registry import Registry

    class Fake:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

        def embed(self, texts):
            return texts

    for module, name in ((cognita.store, "Store"), (cognita.embeddings, "Embedder"),
                         (cognita.embeddings, "Reranker"), (cognita.retrieval, "RetrievalCore"),
                         (cognita.engine_local, "LocalEngineHost")):
        monkeypatch.setattr(module, name, Fake)
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml", data_root=tmp_path / "data")
    store = AccelerationStore(tmp_path / "acceleration.yaml", probe=NvidiaProbe())
    host = _build_engine_host(config, Registry(config.registry_path), store)
    scheduler = host.args[2].kwargs["scheduler"]
    assert isinstance(scheduler, IndexScheduler)
    assert store._scheduler_cards == scheduler.card_states


def test_before_verify_the_display_card_is_not_shown_ready(tmp_path: Path, monkeypatch):
    """kei, "all cards": the display card has under a gigabyte free.  Indexing's
    memory gate and OCR's admission never use it; before Verify the page
    must not call it ready, and must agree with what Verify would say."""
    _runtimes(monkeypatch)

    class TwoCards:
        def devices(self):
            return [
                GpuDevice(sysfs_name="card1", pci_address="0000:03:00.0", unique_id="0123abcd",
                          name="AMD big", vram_total=32 * 1024**3, vram_free=30 * 1024**3, busy_percent=0),
                GpuDevice(sysfs_name="card2", pci_address="0000:0c:00.0", unique_id="4567ef01",
                          name="AMD display", vram_total=2 * 1024**3, vram_free=int(0.98 * 1024**3),
                          busy_percent=3),
            ]

    legacy = tmp_path / "cognita.yaml"
    legacy.write_text("gpu_enabled: true\nocr_device: gpu\n", encoding="utf-8")
    store = AccelerationStore(tmp_path / "acceleration.yaml", legacy_config_path=legacy, probe=TwoCards())
    status = store.status(profile="amd")
    assert status["effective"] == {"knowledge_gpu": True, "ocr_device": "gpu", "fallback_reasons": []}
    big, display = status["cards"]
    assert big["knowledge_usable"] is True and big["ocr_usable"] is True
    assert display["knowledge_usable"] is False and display["ocr_usable"] is False
    assert display["reasons"] == ["insufficient_vram"]


def test_before_verify_a_lone_card_too_small_says_so(tmp_path: Path, monkeypatch):
    _runtimes(monkeypatch)

    class Small:
        def devices(self):
            return [GpuDevice(sysfs_name="gpu0", pci_address=_NVIDIA_PCI, unique_id=_NVIDIA_UUID[4:],
                              name="small", vram_total=2 * 1024**3, vram_free=1 * 1024**3, busy_percent=0)]

    legacy = tmp_path / "cognita.yaml"
    legacy.write_text("gpu_enabled: true\n", encoding="utf-8")
    store = AccelerationStore(tmp_path / "acceleration.yaml", legacy_config_path=legacy, probe=Small())
    assert store.status(profile="nvidia")["effective"] == {
        "knowledge_gpu": False, "ocr_device": "cpu", "fallback_reasons": ["insufficient_vram"]}


def test_after_a_verify_timeout_a_card_indexing_has_used_is_still_reported_in_use(tmp_path: Path, monkeypatch):
    """Verify's own worker can time out (or skip a card while ComfyUI holds its
    memory) while indexing's canary passed on that card in this process."""
    _runtimes(monkeypatch)
    result = _verification_result(reason="verification_timeout", pci=_NVIDIA_PCI, provider="CUDAExecutionProvider")
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(), verifier=FakeAccelerationVerifier(result),
    )
    store.set_scheduler_cards(lambda: [{"device": "gpu0", "state": "cold", "reason": "idle linger elapsed", "has_run": True}])
    store.verify(profile="nvidia", deadline_seconds=5)
    status = store.status(profile="nvidia")
    assert status["effective"]["knowledge_gpu"] is True
    assert status["cards"][0]["knowledge_usable"] is True
    # OCR does not go through indexing's canary: Verify's timeout still decides it.
    assert status["effective"]["ocr_device"] == "cpu"
    assert "verification_timeout" in status["effective"]["fallback_reasons"]


def test_a_card_the_scheduler_has_no_handler_for_is_not_used(tmp_path: Path, monkeypatch):
    """The probe failed when the engine was built, so indexing has no handler
    for any card; the page must not claim one because it can see them now."""
    _runtimes(monkeypatch)
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path), probe=NvidiaProbe(),
    )
    store.set_scheduler_cards(lambda: [])
    status = store.status(profile="nvidia")
    assert status["effective"]["knowledge_gpu"] is False
    assert status["effective"]["ocr_device"] == "cpu"
    assert "telemetry_unavailable" in status["effective"]["fallback_reasons"]


def test_with_no_ocr_list_ocr_uses_the_cards_knowledge_is_pinned_to(tmp_path: Path, monkeypatch):
    """`SchedulerOCRCapacityGate.attempt_devices` resolves the Knowledge list
    when OCR names no card, so a card Knowledge does not use is not OCR's."""
    _runtimes(monkeypatch)

    class TwoBig:
        def devices(self):
            return [
                GpuDevice(sysfs_name="card1", pci_address="0000:03:00.0", unique_id="0123abcd",
                          name="A", vram_total=32 * 1024**3, vram_free=30 * 1024**3, busy_percent=0),
                GpuDevice(sysfs_name="card2", pci_address="0000:0c:00.0", unique_id="4567ef01",
                          name="B", vram_total=32 * 1024**3, vram_free=30 * 1024**3, busy_percent=0),
            ]

    legacy = tmp_path / "cognita.yaml"
    legacy.write_text("gpu_enabled: true\ngpu_device_ids: ['0000:03:00.0']\nocr_device: gpu\n", encoding="utf-8")
    store = AccelerationStore(tmp_path / "acceleration.yaml", legacy_config_path=legacy, probe=TwoBig())
    a, b = store.status(profile="amd")["cards"]
    assert a["ocr_selected"] is True and a["ocr_usable"] is True
    assert b["ocr_selected"] is False and b["ocr_usable"] is False


def test_verify_on_nvidia_makes_the_card_usable_and_passes_the_profile_through(tmp_path: Path):
    verifier = FakeAccelerationVerifier(
        _verification_result(pci=_NVIDIA_PCI, provider="CUDAExecutionProvider")
    )
    store = AccelerationStore(
        tmp_path / "acceleration.yaml", legacy_config_path=_nvidia_legacy(tmp_path),
        probe=NvidiaProbe(), verifier=verifier,
    )
    result = store.verify(profile="nvidia", deadline_seconds=5)
    assert verifier.calls[0][2] == "nvidia"
    assert result["state"] == "passed"
    assert result["effective"] == {"knowledge_gpu": True, "ocr_device": "gpu", "fallback_reasons": []}
    card = store.status(profile="nvidia")["cards"][0]
    assert card["knowledge_usable"] is True and card["ocr_usable"] is True


def test_status_keeps_the_amd_and_cpu_answers_unchanged(tmp_path: Path):
    store = AccelerationStore(tmp_path / "acceleration.yaml", probe=Probe())
    amd = store.status(profile="amd")
    assert amd["deployment"]["profile"] == "amd"
    assert amd["deployment"]["devices_exposed"] is True
    assert amd["deployment"]["vendor_label"] == "AMD"
    assert amd["runtimes"]["embedding"]["expected_provider"] == "MIGraphXExecutionProvider"
    assert amd["cards"][0]["gpu_uuid"] == "GPU-0123abcd"
    cpu = store.status(profile="cpu")
    assert cpu["deployment"]["profile"] == "cpu"
    assert cpu["deployment"]["devices_exposed"] is False
    assert cpu["deployment"]["vendor_label"] is None
    assert "profile_cpu" in cpu["effective"]["fallback_reasons"]
    # A cpu profile resolves GPU settings as amd did before 15.0.
    assert cpu["runtimes"]["embedding"]["expected_provider"] == "MIGraphXExecutionProvider"
    assert cpu["cards"][0]["reasons"][0] == "profile_cpu"
    # An unknown name is the cpu profile, never an exception.
    bogus = store.status(profile="bogus")
    assert bogus["deployment"]["profile"] == "cpu"
    assert bogus["deployment"]["devices_exposed"] is False


async def test_the_admin_route_takes_its_profile_from_the_environment(tmp_path: Path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from cognita.admin_api import create_admin_app
    from cognita.config import CognitaConfig
    from cognita.registry import Registry

    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "nvidia")
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml", data_root=tmp_path / "data",
        admin_allowed_hosts=["*"],
    )
    store = AccelerationStore(tmp_path / "acceleration.yaml", probe=NvidiaProbe())
    app = create_admin_app(config, Registry(config.registry_path), acceleration_store=store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/settings/gpu-acceleration")).json()
    assert body["deployment"]["profile"] == "nvidia"
    assert body["deployment"]["devices_exposed"] is True
    assert body["runtimes"]["embedding"]["expected_provider"] == "CUDAExecutionProvider"
    assert body["cards"][0]["gpu_uuid"] == _NVIDIA_UUID

    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "not-a-profile")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/settings/gpu-acceleration")).json()
    assert body["deployment"]["profile"] == "cpu"


# ---- the verifier: provider by profile, settings passed through, driver_too_old

class _CudaWorker(_FakeGpuWorker):
    def __init__(self, card, config, probe):
        super().__init__(card, config, probe)
        self.stats = SimpleNamespace(provider_active="CUDAExecutionProvider,CPUExecutionProvider")


def _knowledge_only_policy(card):
    from cognita.acceleration import AccelerationPolicy, KnowledgeAcceleration, OcrAcceleration

    return AccelerationPolicy(
        revision=1,
        knowledge=KnowledgeAcceleration(gpu_enabled=True, gpu_device_ids=[card.pci_address]),
        ocr=OcrAcceleration(device="cpu"),
    )


def _verify_with(worker_factory, *, profile: str, config=None):
    card = NvidiaProbe().devices()[0]
    verifier = RealAccelerationVerifier(
        config or SimpleNamespace(embedding_dimensions=3), NvidiaProbe(), worker_factory=worker_factory,
    )
    return verifier.verify(policy=_knowledge_only_policy(card), cards=[card], profile=profile, deadline=10**9)


def test_verifier_expects_the_cuda_provider_on_nvidia_and_migraphx_on_amd():
    on_nvidia = _verify_with(_CudaWorker, profile="nvidia")
    assert on_nvidia["embedding"]["ready"] is True
    assert on_nvidia["embedding"]["provider"] == "CUDAExecutionProvider"
    assert on_nvidia["embedding"]["cards"][0]["active_providers"] == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    # MIGraphX active under the nvidia profile is the WRONG provider: fail, do not pass.
    wrong = _verify_with(_FakeGpuWorker, profile="nvidia")
    assert wrong["embedding"]["ready"] is False
    assert wrong["embedding"]["reason"] == "provider_cpu_fallback"
    # And the reverse: CUDA under the amd profile.
    on_amd = _verify_with(_CudaWorker, profile="amd")
    assert on_amd["embedding"]["ready"] is False
    assert on_amd["embedding"]["provider"] == "MIGraphXExecutionProvider"
    assert on_amd["embedding"]["reason"] == "provider_cpu_fallback"


def test_verifier_on_the_cpu_profile_starts_no_worker_and_says_profile_cpu():
    started: list[str] = []
    result = _verify_with(_recording_factory(started), profile="cpu")
    assert started == []
    assert result["embedding"]["reason"] == "profile_cpu"
    assert result["ocr"]["reason"] == "profile_cpu"
    # Unchanged from before 15.0: a cpu profile reports the amd row's provider.
    assert result["embedding"]["provider"] == "MIGraphXExecutionProvider"


def _capturing_factory(seen: list):
    def factory(card, config, probe):
        seen.append(config)
        return _CudaWorker(card, config, probe)
    return factory


def test_worker_config_passes_the_three_provider_settings_through_untouched():
    seen: list = []
    config = SimpleNamespace(
        embedding_dimensions=3, gpu_provider="cuda", gpu_fixed_seq_len=0,
        gpu_program_cache_dir="/somewhere/cache",
    )
    _verify_with(_capturing_factory(seen), profile="nvidia", config=config)
    assert (seen[0].gpu_provider, seen[0].gpu_fixed_seq_len, seen[0].gpu_program_cache_dir) == (
        "cuda", 0, "/somewhere/cache",
    )
    # An explicit value is the operator's, whatever the profile default would be.
    seen.clear()
    config = SimpleNamespace(embedding_dimensions=3, gpu_provider="migraphx", gpu_fixed_seq_len=512,
                             gpu_program_cache_dir="")
    _verify_with(_capturing_factory(seen), profile="nvidia", config=config)
    assert (seen[0].gpu_provider, seen[0].gpu_fixed_seq_len) == ("migraphx", 512)


def test_worker_config_without_a_config_means_the_profiles_values_not_a_hardcoded_amd_shape():
    seen: list = []
    _verify_with(_capturing_factory(seen), profile="nvidia", config=SimpleNamespace(embedding_dimensions=3))
    # "" = the profile's provider, -1 = the profile's sequence length, no cache dir:
    # GpuWorker.start() resolves these to CUDA / 0 / none on the nvidia profile.
    assert seen[0].gpu_provider == ""
    assert seen[0].gpu_fixed_seq_len == -1
    assert seen[0].gpu_program_cache_dir == ""


_ORT_TEXT = "CUDA failure 35: CUDA driver version is insufficient for CUDA runtime version; provider fell to cpu"


class _StartFailsWorker(_FakeGpuWorker):
    reason: str | None = None

    def start(self):
        from cognita.gpu_host import GpuUnavailable

        raise GpuUnavailable(_ORT_TEXT, reason=self.reason)


class _DriverTooOldWorker(_StartFailsWorker):
    reason = "driver_too_old"


def test_a_worker_that_names_driver_too_old_is_reported_as_driver_too_old():
    result = _verify_with(_DriverTooOldWorker, profile="nvidia")
    assert result["embedding"]["ready"] is False
    assert result["embedding"]["reason"] == "driver_too_old"
    assert result["embedding"]["cards"][0]["reason"] == "driver_too_old"
    assert result["cleanup"] == "passed"


def test_the_same_text_without_the_worker_reason_keeps_its_old_category():
    # Control: only `.reason` differs from the test above. The message contains
    # "provider" and "cpu", so the substring mapping files it as before.
    result = _verify_with(_StartFailsWorker, profile="nvidia")
    assert result["embedding"]["reason"] == "provider_cpu_fallback"


def test_safe_category_maps_driver_too_old_first_and_every_output_is_a_known_reason():
    from cognita.acceleration import FALLBACK_REASONS
    from cognita.acceleration_verify import _safe_category
    from cognita.gpu_host import GpuUnavailable

    def far():
        return 0.0

    samples = [
        (GpuUnavailable("x", reason="driver_too_old"), "driver_too_old"),
        (GpuUnavailable("x timeout y"), "verification_timeout"),
        (GpuUnavailable("provider missing"), "provider_cpu_fallback"),
        (GpuUnavailable("no worker environment"), "runtime_missing"),
        (GpuUnavailable("x", reason="construction_failed"), "runtime_missing"),
        (TimeoutError("late"), "verification_timeout"),
        (RuntimeError("boom"), "runtime_missing"),
    ]
    for exc, expected in samples:
        got = _safe_category(exc, deadline=10**9, clock=far)
        assert got == expected, (exc, got)
        assert got in FALLBACK_REASONS
    assert "driver_too_old" in FALLBACK_REASONS


def test_the_detected_card_row_shows_the_uuid_the_ocr_box_asks_for():
    """15.0 review: the OCR card box takes `GPU-<uuid>` (for NVIDIA the dashed NVML
    form), but the card row showed only name, PCI address and VRAM, so a user had
    to run `nvidia-smi -L` elsewhere to fill it in. Design §15 step 6 expects the
    row to carry the UUID."""
    js = (Path(__file__).resolve().parents[1] / "src/cognita/web/app.js").read_text(encoding="utf-8")
    row = next(line for line in js.splitlines() if "card.gpu_uuid" in line)
    assert "card.gpu_uuid" in row and "esc(card.gpu_uuid)" in row
    assert 't("admin.gpu.detected_cards")' in row
    html = (Path(__file__).resolve().parents[1] / "src/cognita/web/index.html").read_text(encoding="utf-8")
    assert 'data-i18n="admin.gpu.uuid_priority"' in html

def test_the_admin_page_has_words_for_every_reason_the_status_can_report():
    """15.0.3: the page printed raw tokens (runtime_missing, model_unqualified)."""
    from cognita.acceleration import FALLBACK_REASONS
    from cognita.localization import load_catalog

    source = (Path(__file__).resolve().parents[1] / "src" / "cognita" / "web" / "app.js").read_text(encoding="utf-8")
    catalog = load_catalog("en-US")
    table = source[source.index("const GPU_REASON_IDS"):source.index("});", source.index("const GPU_REASON_IDS"))]
    mapped = {reason for reason in FALLBACK_REASONS if f'{reason}: "admin.gpu.reason.{reason}"' in table}
    assert mapped == FALLBACK_REASONS, sorted(FALLBACK_REASONS - mapped)
    assert all(catalog[f"admin.gpu.reason.{reason}"] != reason for reason in FALLBACK_REASONS)
    assert 'const id = GPU_REASON_IDS[reason];' in source and 'return id ? t(id) : reason;' in source
    assert 't("admin.gpu.unverified")' in source
    assert "not checked since restart" in catalog["admin.gpu.unverified"]
