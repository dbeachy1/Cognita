"""Synthetic KEI acceptance for the host-owned GPU scheduler.

Run only on the reference KEI host.  It uses the existing ROCm worker venv and
model cache, creates five synthetic documents under one owned temp root, and
never opens Cognita's configured store, registry, or corpus paths.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from cognita.embeddings import Embedder
from cognita.gpu_probe import GpuDevice, batch_ceiling_gb, default_probe
from cognita.index_scheduler import IndexScheduler, SchedulerSettings, gpu_handlers_from_host
from cognita.retrieval import RetrievalCore


class _TwoGpuProbe:
    """Keep only the two full-size AMD cards; exclude the small display GPU."""

    def __init__(self, devices: list[GpuDevice]):
        self.devices_seen = [device for device in devices if device.vram_total_gb >= 8][:2]

    def devices(self) -> list[GpuDevice]:
        return list(self.devices_seen)


class _EmptyStore:
    async def dense_search(self, project, embedding, limit, category=None):
        return []

    async def lexical_search(self, project, query, limit, category=None):
        return []

    async def registered_lexical_search(self, project, query, limit, category=None):
        return []


def _config(gpu_python: str, model_cache: str) -> SimpleNamespace:
    return SimpleNamespace(
        gpu_enabled=True,
        gpu_venv_python=gpu_python,
        embedding_model="BAAI/bge-large-en-v1.5",
        embedding_dimensions=1024,
        models_cache_dir=model_cache,
        gpu_model_cache_dir=model_cache,
        gpu_program_cache_dir="",
        gpu_provider="migraphx",
        gpu_device_ids=[],
        gpu_cards="all",
        gpu_batch_size=4,
        gpu_slice_chunks=4,
        gpu_fixed_seq_len=512,
        gpu_reserve_vram_gb=4.0,
        gpu_max_busy_percent=95,
        gpu_worker_startup_timeout_s=300.0,
        gpu_worker_shutdown_s=10.0,
        gpu_worker_slice_timeout_s=120.0,
        gpu_canary_tolerance=1e-4,
    )


async def run(gpu_python: str, model_cache: str) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="cognita-kei-scheduler-accept-", dir="/tmp"))
    scheduler: IndexScheduler | None = None
    try:
        documents: list[list[str]] = []
        for project_index in range(5):
            path = root / f"project-{project_index}.txt"
            chunks = [
                f"project-{project_index} cpu-start synthetic chunk",
                f"project-{project_index} gpu chunk zero " + ("bounded synthetic text " * 12),
                f"project-{project_index} gpu chunk one " + ("ordered synthetic text " * 12),
            ]
            path.write_text("\n".join(chunks), encoding="utf-8")
            documents.append(path.read_text(encoding="utf-8").splitlines())

        cfg = _config(gpu_python, model_cache)
        embedder = Embedder(
            cfg.embedding_model,
            cfg.embedding_dimensions,
            Path(cfg.models_cache_dir),
            threads=0,
            batch_size=cfg.gpu_batch_size,
        )
        cpu_embed = embedder.embed
        all_texts = [text for chunks in documents for text in chunks]
        reference = dict(zip(all_texts, cpu_embed(all_texts), strict=True))

        probe = _TwoGpuProbe(default_probe().devices())
        if len(probe.devices_seen) != 2:
            raise RuntimeError(f"expected two full-size AMD GPUs, found {len(probe.devices_seen)}")
        handlers = gpu_handlers_from_host(
            cfg, probe, cpu_embed, batch_ceiling_gb(cfg.gpu_batch_size), holder="kei-scheduler-acceptance"
        )
        if len(handlers) != 2:
            raise RuntimeError(f"expected two scheduler GPU handlers, found {len(handlers)}")

        intervals: list[tuple[str, float, float]] = []
        cpu_started = asyncio.Event()

        async def monitored_cpu(texts):
            cpu_started.set()
            return await asyncio.to_thread(cpu_embed, texts)

        gpu_started = asyncio.Event()
        calls_by_device: dict[str, int] = {handler.device_id: 0 for handler in handlers}
        for handler in handlers:
            original = handler.embed
            device_id = handler.device_id

            async def monitored(texts, *, _original=original, _device_id=device_id):
                calls_by_device[_device_id] += 1
                gpu_started.set()
                started = time.monotonic()
                try:
                    return await _original(texts)
                finally:
                    intervals.append((_device_id, started, time.monotonic()))

            handler.embed = monitored
            handler.available = False
            handler.state = "unavailable"

        scheduler = IndexScheduler(
            monitored_cpu,
            gpu_devices=handlers,
            settings=SchedulerSettings(
                max_chunks=8,
                max_text_bytes=16 * 1024 * 1024,
                cpu_max_chunk_bytes=64,
                gpu_retry_cooldown_s=0.0,
            ),
            dimensions=cfg.embedding_dimensions,
        )
        jobs = [scheduler.open_job(f"synthetic-project-{i}", estimated_chunks=3)
                for i in range(5)]
        tasks = [asyncio.create_task(scheduler.embed(job, chunks))
                 for job, chunks in zip(jobs, documents, strict=True)]

        # CPU starts while both GPUs are unavailable; readiness then admits the
        # same jobs to real GPU workers without a per-job lease.
        await asyncio.wait_for(cpu_started.wait(), timeout=30.0)
        scheduler.mark_gpu_ready(handlers[0].device_id)
        scheduler.mark_gpu_ready(handlers[1].device_id)
        await asyncio.wait_for(gpu_started.wait(), timeout=600.0)
        health = scheduler.snapshot()
        if health["jobs"] or not health["gpus"]:
            raise AssertionError("public health snapshot exposed job identifiers or no GPUs")
        if health["queued"] + health["in_flight"] <= 0:
            raise AssertionError("public health snapshot did not report active aggregate work")

        # Query/rerank embedding bypasses indexing dispatch while GPU work is active.
        query_core = RetrievalCore(_EmptyStore(), embedder, scheduler=scheduler)
        before = scheduler.snapshot()["queued"] + scheduler.snapshot()["in_flight"]
        await query_core.search("synthetic-project-0", "synthetic query", hybrid_alpha=1.0)
        after = scheduler.snapshot()["queued"] + scheduler.snapshot()["in_flight"]
        if after > before:
            raise AssertionError("retrieval query manufactured scheduler indexing work")

        results = await asyncio.gather(*tasks)
        for chunks, vectors in zip(documents, results, strict=True):
            for text, vector in zip(chunks, vectors, strict=True):
                expected = reference[text]
                delta = max(abs(float(actual) - float(want))
                            for actual, want in zip(vector, expected, strict=True))
                if not math.isfinite(delta) or delta > cfg.gpu_canary_tolerance:
                    raise AssertionError(f"GPU/CPU vector delta exceeded tolerance: {delta}")

        # Inject one recoverable device failure after the real workers have run;
        # with the peer unavailable, two attempts must fall back to CPU.
        failing = handlers[0]
        original = failing.embed
        failures = 0

        async def injected_failure(texts):
            nonlocal failures
            failures += 1
            raise RuntimeError("synthetic OOM for acceptance")

        failing.embed = injected_failure
        scheduler.quarantine_gpu(handlers[1].device_id, "synthetic admission failover")
        fallback_job = scheduler.open_job("synthetic-fallback")
        fallback_vectors = await scheduler.embed(fallback_job, documents[0][1:2])
        if fallback_vectors != [reference[documents[0][1]]]:
            raise AssertionError("CPU fallback vector did not preserve exact association")
        fallback_snapshot = scheduler.snapshot("synthetic-fallback")["jobs"][0]
        if fallback_snapshot["device_completed"] != {"cpu": 1} or failures != 2:
            raise AssertionError(f"unexpected fallback accounting: {fallback_snapshot}")
        failing.embed = original
        return {
            "temp_root": str(root),
            "gpu_devices": list(calls_by_device),
            "gpu_calls": sum(calls_by_device.values()),
            "overlap": any(
                left[1] < right[2] and right[1] < left[2]
                for index, left in enumerate(intervals)
                for right in intervals[index + 1:]
                if left[0] != right[0]
            ),
            "fallback_failures": failures,
            "health_gpu_states": [gpu["state"] for gpu in health["gpus"]],
        }
    finally:
        if scheduler is not None:
            await scheduler.shutdown()
        shutil.rmtree(root)
        if root.exists():
            raise RuntimeError(f"owned acceptance temp root was not removed: {root}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-python", default=str(Path.home() / "Cognita" / "gpu-venv" / "bin" / "python"))
    parser.add_argument("--model-cache", default=str(Path.home() / "Cognita" / "models_cache"))
    args = parser.parse_args()
    result = asyncio.run(run(args.gpu_python, args.model_cache))
    print(result)


if __name__ == "__main__":
    main()
