from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognita.assets.models import AssetError
from cognita.assets.ocr_models import OCRRegion, OCRWorkerPayload
from cognita.assets.ocr_service import (
    OCRService,
    SchedulerOCRCapacityGate,
    _cpu_headroom_available,
    _ocr_timeout_seconds,
    configured_pipeline_identity,
    effective_dimension_limit,
)
from cognita.assets.ocr_worker import OCRWorkerError
from cognita.assets.service import AssetService
from cognita.config import CognitaConfig
from cognita.gpu_probe import GIB, GpuDevice
from cognita.index_scheduler import DeviceHandler


class FakeRunner:
    def __init__(self, payload: OCRWorkerPayload):
        self.payload = payload
        self.seen: list[tuple[bytes, tuple[str, ...]]] = []

    async def run(self, image: bytes, languages: tuple[str, ...]) -> OCRWorkerPayload:
        self.seen.append((image, languages))
        return self.payload


@pytest.mark.asyncio
async def test_ocr_search_freshness_uses_timestamp_preserving_reader(tmp_path, monkeypatch):
    import hashlib
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    source = tmp_path / "screen.png"
    source.write_bytes(fixture)
    digest = hashlib.sha256(fixture).hexdigest()

    class Repository:
        async def search_hybrid(self, *args):
            return [{"source": "screen.png", "provenance": "ocr", "ocr_source_sha256": digest}]

    service = AssetService(_project(tmp_path), Repository(), limits=_config())
    original = service.ocr_service._snapshot
    snapshots = []
    def snapshot(target, filepath):
        snapshots.append((target, filepath))
        return original(target, filepath)
    monkeypatch.setattr(service.ocr_service, "_snapshot", snapshot)
    monkeypatch.setattr(service, "_read_png", lambda target: pytest.fail("ordinary asset reader can advance OCR source atime"))
    found = await service.search_assets({"query": "searchable", "hybrid_alpha": 0})
    assert snapshots == [(source, "screen.png")]
    assert found["results"][0]["provenance"] == "ocr"
    source.write_bytes(Path("tests/fixtures/ocr_qualification/blank.png").read_bytes())
    stale = await service.search_assets({"query": "searchable", "hybrid_alpha": 0})
    assert stale["results"] == []
    assert stale["warnings"][0]["code"] == "ocr_freshness"


def _config(**overrides):
    values = {
        "ocr_require_noatime": False,
        "ocr_max_png_bytes": 16 * 1024 * 1024,
        "ocr_max_pixels": 16_777_216,
        "ocr_max_dimension": 8_192,
        "ocr_max_result_bytes": 1 * 1024 * 1024,
        "ocr_max_regions": 10_000,
        "ocr_timeout_s": 2,
        "ocr_queue_timeout_s": 0.1,
        "ocr_concurrency": 1,
        "ocr_queue_size": 4,
        "ocr_cpu_threads": 2,
        "ocr_low_confidence": 0.60,
        "ocr_enabled_languages": ["en"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _project(root: Path):
    return SimpleNamespace(name="Synthetic", documents_dir=root, data_dir=root / ".data")


@pytest.mark.asyncio
async def test_ocr_normalizes_order_and_preserves_snapshot(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    path = tmp_path / "screen.png"
    path.write_bytes(fixture)
    payload = OCRWorkerPayload(
        1600, 424,
        (
            OCRRegion("Second line", (10, 80, 210, 110), ((10, 80), (210, 80), (210, 110), (10, 110)), 0.99, 0, 0, 0),
            OCRRegion("First line", (10, 20, 210, 50), ((10, 20), (210, 20), (210, 50), (10, 50)), 0.95, 0, 0, 0),
        ),
        "cpu", "pytorch-cpu", "easyocr", "1.7.2", "m" * 64,
    )
    runner = FakeRunner(payload)
    result = await OCRService(_config(), runner=runner).extract(
        _project(tmp_path), "screen.png", target=path,
    )
    assert result["outcome"] == "text"
    assert result["text"] == "First line\nSecond line"
    assert [r["order"] for r in result["regions"]] == [0, 1]
    assert result["sha256"] == __import__("hashlib").sha256(fixture).hexdigest()
    assert runner.seen == [(fixture, ("en",))]
    assert path.read_bytes() == fixture
    # 13.0.2: the reported limit is the ENFORCED one. The config says 8,192
    # but the scan clamps to the asset store's 4,096, and the over-limit
    # refusal names 4,096 — a successful result must say the same number.
    assert result["limits"]["max_dimension"] == 4_096


@pytest.mark.asyncio
async def test_local_ocr_accepts_png_above_inline_response_limit(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    padding = 2 * 1_048_576 - len(fixture) - 12
    kind = b"tEXt"
    payload = b"x" * padding
    chunk = (
        struct.pack(">I", padding) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )
    image = fixture[:-12] + chunk + fixture[-12:]
    path = tmp_path / "large-screen.png"
    path.write_bytes(image)
    runner = FakeRunner(OCRWorkerPayload(
        1600, 424, (), "cpu", "pytorch-cpu", "easyocr", "1.7.2", "m" * 64,
    ))

    result = await OCRService(_config(), runner=runner).extract(
        _project(tmp_path), "large-screen.png", target=path,
    )

    assert result["status"] == "success"
    assert result["source_snapshot"]["file_size"] == 2 * 1_048_576
    assert runner.seen == [(image, ("en",))]


@pytest.mark.asyncio
async def test_ocr_over_limit_dimensions_keep_too_large_reason_and_explain_limit(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    ihdr = (
        struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">IIBBBBB", 5_000, 1, 8, 6, 0, 0, 0)
        + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 5_000, 1, 8, 6, 0, 0, 0)) & 0xFFFFFFFF)
    )
    image = fixture[:8] + ihdr + fixture[33:]
    path = tmp_path / "over-limit.png"
    path.write_bytes(image)
    runner = FakeRunner(OCRWorkerPayload(1600, 424, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64))

    with pytest.raises(AssetError) as caught:
        await OCRService(_config(ocr_max_dimension=4_096), runner=runner).extract(
            _project(tmp_path), "over-limit.png", target=path,
        )

    assert caught.value.reason == "too_large"
    assert "4,096-pixel" in caught.value.message
    assert runner.seen == []


def _forged_width_png(width: int) -> bytes:
    """The canonical clear fixture with its IHDR width overwritten in place."""
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    body = struct.pack(">IIBBBBB", width, 1, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", 13) + b"IHDR" + body + struct.pack(">I", zlib.crc32(b"IHDR" + body) & 0xFFFFFFFF)
    return fixture[:8] + ihdr + fixture[33:]


# ---------------------------------------------------------------------------
# 12.18.5 / 13.0.2 LIVE BUG: the enforced OCR dimension limit is min(config, 4096),
# always -- but limits.max_dimension (both here and in assets/service.py's
# cache-hit projection) and the worker's own request advertised/enforced
# the raw, uncapped config value (8192 by default) instead of the number
# scan-time admission actually enforces.
# ---------------------------------------------------------------------------


def test_effective_dimension_limit_caps_the_raw_config_value():
    assert effective_dimension_limit(_config()) == 4_096
    # A config already below the hard ceiling is honored as-is.
    assert effective_dimension_limit(_config(ocr_max_dimension=2_000)) == 2_000
    # A missing config attribute falls back to the 8192 default and is then
    # capped the same way.
    assert effective_dimension_limit(SimpleNamespace()) == 4_096


@pytest.mark.asyncio
async def test_ocr_result_limits_advertise_the_enforced_dimension_not_the_raw_config(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    path = tmp_path / "screen.png"
    path.write_bytes(fixture)
    runner = FakeRunner(OCRWorkerPayload(
        1600, 424, (), "cpu", "pytorch-cpu", "easyocr", "1.7.2", "m" * 64,
    ))
    # Default config's ocr_max_dimension is the raw 8192 -- the response
    # must still say 4096, the number actually enforced.
    result = await OCRService(_config(), runner=runner).extract(
        _project(tmp_path), "screen.png", target=path,
    )
    assert result["limits"]["max_dimension"] == 4_096


@pytest.mark.asyncio
async def test_ocr_over_limit_dimensions_refused_at_the_advertised_limit_with_default_config(tmp_path):
    # Even with the raw config at its 8192 default (nowhere near 5000), a
    # 5000px-wide image is refused at 4096 -- the SAME number
    # limits.max_dimension advertises, not silently accepted because the
    # raw config value alone is bigger than 5000.
    path = tmp_path / "over-limit-default.png"
    path.write_bytes(_forged_width_png(5_000))
    runner = FakeRunner(OCRWorkerPayload(1600, 424, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64))

    with pytest.raises(AssetError) as caught:
        await OCRService(_config(), runner=runner).extract(
            _project(tmp_path), "over-limit-default.png", target=path,
        )

    assert caught.value.reason == "too_large"
    assert "4,096-pixel" in caught.value.message
    assert runner.seen == []


@pytest.mark.asyncio
async def test_asset_ocr_rejects_language_and_path_before_runner(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(Path("tests/fixtures/ocr_qualification/blank.png").read_bytes())
    runner = FakeRunner(OCRWorkerPayload(100, 100, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64))
    service = AssetService(_project(tmp_path), limits=_config(), ocr_service=OCRService(_config(), runner=runner))
    with pytest.raises(AssetError) as unsupported:
        await service.ocr_asset({"filepath": "a.png", "languages": ["fr"]})
    assert unsupported.value.reason == "unsupported_language"
    with pytest.raises(AssetError) as traversal:
        await service.ocr_asset({"filepath": "../a.png"})
    assert traversal.value.reason == "invalid_path"
    assert runner.seen == []


@pytest.mark.asyncio
async def test_asset_ocr_publishes_search_chunks_and_reuses_qualified_cache(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    path = tmp_path / "screen.png"
    path.write_bytes(fixture)
    manifest = tmp_path / "qualification.json"
    manifest.write_text(json.dumps({
        "engine": "easyocr==1.7.2",
        "model_files": {"aggregate_sha256": "a" * 64},
    }), encoding="utf-8")
    config = _config(ocr_qualification_manifest=str(manifest), chunk_size=20, chunk_overlap=4)
    model_fingerprint, _pipeline = configured_pipeline_identity(config, ("en",))
    runner = FakeRunner(OCRWorkerPayload(
        1600, 424,
        (OCRRegion("searchable OCR token", (10, 20, 210, 50),
                   ((10, 20), (210, 20), (210, 50), (10, 50)),
                   0.99, 0, 0, 0),),
        "cpu", "pytorch-cpu", "easyocr", "1.7.2", model_fingerprint,
    ))

    class Repository:
        def __init__(self):
            self.result = None
            self.chunks = []
            self.published = 0

        async def get_ocr_result(self, source_sha256, pipeline_fingerprint, languages_key):
            if self.result is not None and self.result.cache_key == (
                source_sha256, pipeline_fingerprint, languages_key,
            ):
                return self.result
            return None

        async def get_ocr_chunks(self, source_sha256, pipeline_fingerprint, languages_key):
            return self.chunks

        async def validate_ocr_freshness(self, filepath, source_sha256):
            return True

        async def publish_ocr_result(self, snapshot, result, chunks):
            self.result = result
            self.chunks = list(chunks)
            self.published += 1

    class Embedder:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            return [[float(len(text))] for text in texts]

    repository = Repository()
    embedder = Embedder()
    service = AssetService(
        _project(tmp_path), repository, limits=config, embedder=embedder,
        ocr_service=OCRService(config, runner=runner),
    )

    first = await service.ocr_asset({"project": "Synthetic", "filepath": "screen.png"})
    second = await service.ocr_asset({"project": "Synthetic", "filepath": "screen.png"})

    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert first["text"] == second["text"] == "searchable OCR token"
    assert runner.seen == [(fixture, ("en",))]
    assert embedder.calls == 1
    # 12.18.5: assets/service.py's cache-hit `limits` projection is a
    # SEPARATE site from ocr_service.py's fresh-run one -- both must report
    # the enforced 4096, not the raw config's 8192 default.
    assert first["limits"]["max_dimension"] == 4_096
    assert second["limits"]["max_dimension"] == 4_096
    assert repository.chunks and repository.chunks[0].text
    assert repository.published == 2  # inference publication + cache source remap
    assert "source_snapshot" not in first and "pipeline_fingerprint" not in first


@pytest.mark.asyncio
async def test_ocr_coalesces_matching_computation_and_releases_last_owner():
    service = OCRService(_config())
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "done"

    first = asyncio.create_task(
        service.coalesce(("P", "hash"), factory, timeout_seconds=1),
    )
    await entered.wait()
    second = asyncio.create_task(
        service.coalesce(("P", "hash"), factory, timeout_seconds=1),
    )
    release.set()
    assert await asyncio.gather(first, second) == ["done", "done"]
    assert calls == 1 and service._coalesced == {}


@pytest.mark.asyncio
async def test_scheduler_ocr_gate_uses_fresh_vram_and_shared_device_turn():
    device = GpuDevice("card1", "0000:03:00.0", "abc", "R9700", 32 * GIB, 10 * GIB, 0)

    class Probe:
        def __init__(self):
            self.device = device

        def devices(self):
            return [self.device]

    handler = DeviceHandler("card1", lambda texts: [])
    scheduler = SimpleNamespace(gpus=[handler], cpu=DeviceHandler("cpu", lambda texts: []))
    config = _config(
        ocr_gpu_device_id="GPU-abc", ocr_gpu_required_vram_mb=4096,
        gpu_reserve_vram_gb=4.0,
    )
    probe = Probe()
    gate = SchedulerOCRCapacityGate(config, scheduler, probe)

    assert await gate.qualify(device="gpu", phase="inference", deadline=10**9)
    assert handler._turn.locked()
    await gate.release(device="gpu")
    assert not handler._turn.locked()

    probe.device = GpuDevice("card1", "0000:03:00.0", "abc", "R9700", 32 * GIB, 7 * GIB, 0)
    assert not await gate.qualify(device="gpu", phase="inference", deadline=10**9)
    assert not handler._turn.locked()


def test_ocr_language_identity_is_canonical_and_duplicate_safe():
    service = OCRService(_config(ocr_enabled_languages=["en", "fr"]))
    assert service._languages(["fr", "en"]) == ("en", "fr")
    with pytest.raises(AssetError) as duplicate:
        service._languages(["en", "EN"])
    assert duplicate.value.reason == "unsupported_language"


class _CapacityGate:
    def __init__(self, allowed: dict[str, bool]):
        self.allowed = allowed
        self.qualifications: list[tuple[str, str]] = []
        self.cleaned: list[tuple[str, str]] = []

    async def qualify(self, *, device: str, phase: str, deadline: float):
        self.qualifications.append((device, phase))
        return self.allowed.get(device, False)

    async def wait(self, *, deadline: float):
        self.allowed["gpu"] = True

    async def cleanup(self, *, device: str, reason: str):
        self.cleaned.append((device, reason))


class _AdvancingClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.01
        return self.value


class _PressureGate(_CapacityGate):
    def __init__(self, clock):
        super().__init__({"gpu": False, "cpu": False})
        self.clock = clock

    async def wait(self, *, deadline: float):
        self.clock.value = deadline + 1


class _DeviceRunner:
    def __init__(
        self, payload: OCRWorkerPayload, *, oom_devices: set[str] | None = None,
        oom_bindings: set[str] | None = None,
    ):
        self.payload = payload
        self.oom_devices = oom_devices or set()
        self.oom_bindings = oom_bindings or set()
        self.calls: list[str] = []
        self.bindings: list[str | None] = []

    async def run(
        self, image: bytes, languages: tuple[str, ...], *, timeout_seconds=None,
        device=None, device_id=None,
    ):
        selected = str(device or "cpu")
        self.calls.append(selected)
        self.bindings.append(device_id)
        if selected in self.oom_devices or device_id in self.oom_bindings:
            raise OCRWorkerError("gpu_oom" if selected != "cpu" else "cpu_oom")
        return self.payload


def _two_gpu_runtime(config, *, free_gib: int | tuple[int, int] = 10):
    free = (free_gib, free_gib) if isinstance(free_gib, int) else free_gib
    devices = [
        GpuDevice("card1", "0000:03:00.0", "aaa", "R9700-0", 32 * GIB, free[0] * GIB, 0),
        GpuDevice("card2", "0000:07:00.0", "bbb", "R9700-1", 32 * GIB, free[1] * GIB, 0),
    ]

    class Probe:
        def devices(self):
            return list(devices)

    scheduler = SimpleNamespace(
        gpus=[
            DeviceHandler("card1", lambda texts: []),
            DeviceHandler("card2", lambda texts: []),
        ],
        cpu=DeviceHandler("cpu", lambda texts: []),
    )
    return SchedulerOCRCapacityGate(config, scheduler, Probe())


def test_real_config_ordered_ocr_pins_override_default_card_order():
    ordered = CognitaConfig(
        ocr_device="gpu", gpu_cards="none",
        ocr_gpu_device_ids=["GPU-bbb", "GPU-aaa"],
    )
    singular = CognitaConfig(
        ocr_device="gpu", gpu_cards="all", ocr_gpu_device_id="GPU-bbb",
    )
    assert _two_gpu_runtime(ordered).attempt_devices() == (
        "GPU-bbb", "GPU-aaa", "cpu",
    )
    assert _two_gpu_runtime(singular).attempt_devices() == ("GPU-bbb", "cpu")


@pytest.mark.asyncio
async def test_real_config_gpu0_oom_fails_over_to_gpu1_in_probe_order(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "two-gpu.png"
    path.write_bytes(fixture)
    config = CognitaConfig(
        ocr_device="gpu", gpu_cards="all", ocr_require_noatime=False,
        ocr_gpu_required_vram_mb=4_096, gpu_reserve_vram_gb=4.0,
    )
    gate = _two_gpu_runtime(config)
    payload = OCRWorkerPayload(
        1000, 240, (), "gpu", "pytorch-rocm", "easyocr", "1", "m" * 64,
    )
    runner = _DeviceRunner(payload, oom_bindings={"GPU-aaa"})

    result = await OCRService(config, runner=runner, capacity_gate=gate).extract(
        SimpleNamespace(name="Synthetic"), "two-gpu.png", target=path,
        timeout_seconds=10,
    )

    assert result["status"] == "success"
    assert runner.calls == ["gpu", "gpu"]
    assert runner.bindings == ["GPU-aaa", "GPU-bbb"]
    assert not any(warning["code"] == "gpu_fallback" for warning in result["warnings"])


@pytest.mark.asyncio
async def test_real_config_gpu0_admission_rejection_uses_gpu1(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "gpu1.png"
    path.write_bytes(fixture)
    config = CognitaConfig(
        ocr_device="gpu", gpu_cards="all", ocr_require_noatime=False,
        ocr_gpu_required_vram_mb=4_096, gpu_reserve_vram_gb=4.0,
    )
    runner = _DeviceRunner(OCRWorkerPayload(
        1000, 240, (), "gpu", "pytorch-rocm", "easyocr", "1", "m" * 64,
    ))

    result = await OCRService(
        config, runner=runner,
        capacity_gate=_two_gpu_runtime(config, free_gib=(7, 10)),
    ).extract(
        SimpleNamespace(name="Synthetic"), "gpu1.png", target=path,
        timeout_seconds=10,
    )

    assert result["status"] == "success"
    assert runner.calls == ["gpu"] and runner.bindings == ["GPU-bbb"]
    assert not any(warning["code"] == "gpu_fallback" for warning in result["warnings"])


@pytest.mark.asyncio
async def test_real_config_both_gpus_constrained_uses_gated_cpu(
    tmp_path, monkeypatch,
):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "cpu-fallback.png"
    path.write_bytes(fixture)
    config = CognitaConfig(
        ocr_device="gpu", gpu_cards="all", ocr_require_noatime=False,
        ocr_gpu_required_vram_mb=4_096, gpu_reserve_vram_gb=4.0,
    )
    gate = _two_gpu_runtime(config, free_gib=7)
    monkeypatch.setattr(
        "cognita.assets.ocr_service._cpu_headroom_available",
        lambda _config: True,
    )
    runner = _DeviceRunner(OCRWorkerPayload(
        1000, 240, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64,
    ))

    result = await OCRService(config, runner=runner, capacity_gate=gate).extract(
        SimpleNamespace(name="Synthetic"), "cpu-fallback.png", target=path,
        timeout_seconds=10,
    )

    assert runner.calls == ["cpu"] and runner.bindings == [None]
    assert result["engine"]["device"] == "cpu"
    assert any(warning["code"] == "gpu_fallback" for warning in result["warnings"])


@pytest.mark.asyncio
async def test_ocr_capacity_wait_reprobes_and_keeps_memory_failure_recoverable(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "wait.png"
    path.write_bytes(fixture)
    payload = OCRWorkerPayload(1000, 240, (), "gpu", "pytorch-rocm", "easyocr", "1", "m" * 64)
    gate = _CapacityGate({"gpu": False})
    runner = _DeviceRunner(payload)
    result = await OCRService(_config(ocr_device="gpu", ocr_gpu_devices=("gpu",)), runner=runner,
                              capacity_gate=gate).extract(SimpleNamespace(name="Synthetic"), "wait.png",
                                                          target=path, timeout_seconds=10)
    assert result["outcome"] == "no_text"
    assert runner.calls == ["gpu"]
    assert gate.qualifications[0][0] == "gpu"
    assert gate.qualifications[-1][0] == "gpu"


@pytest.mark.asyncio
async def test_ocr_gpu_oom_falls_back_to_cpu_once_with_warning(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "oom.png"
    path.write_bytes(fixture)
    payload = OCRWorkerPayload(1000, 240, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64)
    gate = _CapacityGate({"gpu": True, "cpu": True})
    runner = _DeviceRunner(payload, oom_devices={"gpu"})
    result = await OCRService(_config(ocr_device="gpu", ocr_gpu_devices=("gpu",)), runner=runner,
                              capacity_gate=gate).extract(SimpleNamespace(name="Synthetic"), "oom.png",
                                                          target=path, timeout_seconds=10)
    assert runner.calls == ["gpu", "cpu"]
    assert result["engine"]["device"] == "cpu"
    assert any(w["code"] == "gpu_fallback" for w in result["warnings"])
    assert gate.cleaned == [("gpu", "gpu_oom")]


def test_ocr_timeout_seconds_rejects_bool_fraction_and_out_of_range():
    for value in (True, 10.5, 9, 601, "10"):
        with pytest.raises(AssetError) as error:
            _ocr_timeout_seconds(value)
        assert error.value.reason == "invalid_arguments"


def test_cpu_headroom_gate_uses_memavailable_and_cgroup_remaining(tmp_path):
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    (proc / "meminfo").write_text("MemAvailable: 4194304 kB\n", encoding="ascii")
    (cgroup / "memory.max").write_text(str(4 * 1024**3), encoding="ascii")
    (cgroup / "memory.current").write_text(str(1 * 1024**3), encoding="ascii")
    config = _config(ocr_worker_memory_mb=2048, ocr_cpu_reserve_ram_mb=1024)

    assert _cpu_headroom_available(config, proc_root=proc, cgroup_root=cgroup, platform="posix")
    (cgroup / "memory.current").write_text(str(2 * 1024**3), encoding="ascii")
    assert not _cpu_headroom_available(config, proc_root=proc, cgroup_root=cgroup, platform="posix")
    (proc / "meminfo").write_text("MemFree: 9999999 kB\n", encoding="ascii")
    assert not _cpu_headroom_available(config, proc_root=proc, cgroup_root=cgroup, platform="posix")


@pytest.mark.asyncio
async def test_ocr_memory_pressure_expires_as_timeout_not_ocr_failed(tmp_path):
    fixture = Path("tests/fixtures/ocr_qualification/blank.png").read_bytes()
    path = tmp_path / "pressure.png"
    path.write_bytes(fixture)
    clock = _AdvancingClock()
    gate = _PressureGate(clock)
    runner = _DeviceRunner(OCRWorkerPayload(1000, 240, (), "cpu", "pytorch-cpu", "easyocr", "1", "m" * 64))
    with pytest.raises(AssetError) as error:
        await OCRService(_config(ocr_device="gpu", ocr_gpu_devices=("gpu",)), runner=runner,
                         capacity_gate=gate, clock=clock).extract(
                             SimpleNamespace(name="Synthetic"), "pressure.png", target=path, timeout_seconds=10)
    assert error.value.reason == "timeout"
    assert error.value.details["phase"] == "capacity_wait"
    assert runner.calls == []


def _stat_with(real, **fields):
    names = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size",
             "st_atime", "st_mtime", "st_ctime")
    values = {name: getattr(real, name) for name in names}
    values.update({name: getattr(real, name) for name in ("st_atime_ns", "st_mtime_ns", "st_ctime_ns")})
    values.update(fields)
    return SimpleNamespace(**values)


def test_ocr_freshness_ignores_host_access_timestamps_but_not_content(tmp_path):
    """2026-09-28, Windows 13.7.0: reading a PNG on the OneDrive DrvFS mount
    moved its atime and ctime, and OCR answered source_changed for a file
    nobody had touched.  Identity, size, mtime and the bytes decide."""
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    source = tmp_path / "screen.png"
    source.write_bytes(fixture)
    service = AssetService(_project(tmp_path), object(), limits=_config()).ocr_service
    snapshot, _facts, _raw = service._snapshot(source, "screen.png")
    # The stored token as a noatime host records it: identity + ctime/atime
    # from an earlier moment than the file now shows.
    stale_times = dict(snapshot, snapshot_token=snapshot["snapshot_token"] + ":1:1", atime_preserved=True)
    service._freshness_check(source, stale_times)

    source.write_bytes(Path("tests/fixtures/ocr_qualification/blank.png").read_bytes())
    with pytest.raises(AssetError) as changed:
        service._freshness_check(source, stale_times)
    assert changed.value.reason == "source_changed"


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOATIME"), reason="O_NOATIME is Linux-only")
def test_ocr_read_tolerates_access_timestamps_moving_during_the_read(tmp_path, monkeypatch, caplog):
    import os
    fixture = Path("tests/fixtures/ocr_qualification/canonical_clear.png").read_bytes()
    source = tmp_path / "screen.png"
    source.write_bytes(fixture)
    service = AssetService(_project(tmp_path), object(), limits=_config(ocr_require_noatime=True)).ocr_service
    real_fstat = os.fstat
    calls = []

    def fstat(descriptor):
        result = real_fstat(descriptor)
        calls.append(1)
        if len(calls) == 2:   # the fstat after the read: the host stamped access
            return _stat_with(result, st_atime_ns=result.st_atime_ns + 10**9,
                              st_ctime_ns=result.st_ctime_ns + 10**9)
        return result

    monkeypatch.setattr(os, "fstat", fstat)
    with caplog.at_level("INFO", logger="cognita.assets.ocr"):
        snapshot, _facts, raw = service._snapshot(source, "screen.png")
    assert raw == fixture
    assert "access timestamps moved" in caplog.text

    calls.clear()

    def resized(descriptor):
        result = real_fstat(descriptor)
        calls.append(1)
        return _stat_with(result, st_size=result.st_size + 1) if len(calls) == 2 else result

    monkeypatch.setattr(os, "fstat", resized)
    with pytest.raises(AssetError) as changed:
        service._snapshot(source, "screen.png")
    assert changed.value.reason == "source_changed"
