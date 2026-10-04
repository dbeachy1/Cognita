"""14.0 §2: the pinned bge-reranker-v2-m3 and its background load.

Everything is faked. No test downloads a model or touches the network, and no
test waits on the clock: the background-load test waits on an Event the fake
constructor sets and blocks on, and its ``timeout=`` arguments are hang guards
for signals that must arrive in a correct run.

The pinned table is patched to point at three tiny fake files whose real sizes
and SHA-256 values are computed here, so `_fetch_pinned` runs its real
verification against them. `huggingface_hub.hf_hub_download` is replaced by a
fake that writes those files
into the `local_dir` it is given and records every call.
"""

import hashlib
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cognita import embeddings
from cognita.config import CognitaConfig
from cognita.embeddings import PinnedModel, Reranker
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import ChunkHit, SchemaVersionMismatch, Store
from fastembed_fakes import _FakeCrossEncoder
from retrieval_fakes import HashEmbedder

PINNED_NAME = "test/pinned-reranker"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REPO = "example/pinned-reranker-onnx"

# relative path -> content. The largest one plays the role of model.onnx_data.
CONTENTS = {
    "config.json": b'{"fake": true}',
    "onnx/model.onnx": b"graph-bytes",
    "onnx/model.onnx_data": b"weights-" * 64,
}


def _spec() -> PinnedModel:
    return PinnedModel(
        repo=REPO,
        revision=REVISION,
        model_file="onnx/model.onnx",
        additional_files=("onnx/model.onnx_data",),
        license="apache-2.0",
        size_in_gb=0.001,
        files={
            rel: (len(data), hashlib.sha256(data).hexdigest())
            for rel, data in CONTENTS.items()
        },
    )


class HubFake:
    """Stands in for `huggingface_hub.hf_hub_download(local_dir=...)`: plain files
    written into the local_dir it is given, as the real one does."""

    def __init__(self):
        self.snapshot: Path | None = None  # the local_dir of the last call
        self.calls: list[dict] = []
        self.corrupt: dict[str, bytes] = {}  # rel -> bytes to write instead
        self.raises: Exception | None = None

    def hf_hub_download(self, repo_id, filename, *, revision=None, local_dir=None):
        self.calls.append(
            {"repo": repo_id, "file": filename, "revision": revision, "local_dir": local_dir}
        )
        if self.raises is not None:
            raise self.raises
        self.snapshot = Path(local_dir)
        target = self.snapshot / filename
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.corrupt.get(filename, CONTENTS[filename]))
        return str(target)


@pytest.fixture
def hub(tmp_path, monkeypatch, fake_fastembed):
    import huggingface_hub

    fake = HubFake()
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake.hf_hub_download)
    monkeypatch.setitem(embeddings.PINNED_RERANKERS, PINNED_NAME, _spec())
    return fake


def _messages(caplog, level=None, logger="cognita.embeddings"):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == logger and (level is None or r.levelno == level)
    ]


# --------------------------------------------------------------------------
# 1. R8: registration
# --------------------------------------------------------------------------


def test_two_rerankers_register_the_pinned_name_exactly_once(hub, tmp_path):
    first = Reranker(PINNED_NAME, tmp_path)
    second = Reranker(PINNED_NAME, tmp_path)
    assert first.rerank("q", ["a"]) == [1.0]
    assert second.rerank("q", ["a"]) == [1.0]

    assert len(_FakeCrossEncoder.custom_models) == 1
    registered = _FakeCrossEncoder.custom_models[0]
    assert registered["model"] == PINNED_NAME
    assert registered["sources"].hf == REPO
    assert registered["model_file"] == "onnx/model.onnx"
    assert registered["additional_files"] == ["onnx/model.onnx_data"]


@pytest.mark.parametrize("spelling", [
    "Xenova/ms-marco-MiniLM-L-6-v2",
    "xenova/MS-MARCO-minilm-l-6-v2",
    "XENOVA/MS-MARCO-MINILM-L-6-V2",
])
def test_a_name_fastembed_already_lists_is_never_registered(
    hub, tmp_path, monkeypatch, spelling, caplog
):
    # The fake's list_supported_models returns DICTS, as fastembed's does, so a
    # `name in list` check would miss all of these.
    monkeypatch.setitem(embeddings.PINNED_RERANKERS, spelling, _spec())
    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        assert Reranker(spelling, tmp_path).rerank("q", ["a"]) == [1.0]

    assert _FakeCrossEncoder.custom_models == []
    assert any("already knows the name" in m for m in _messages(caplog, logging.INFO))


# --------------------------------------------------------------------------
# 2. R9: the pin reaches Hugging Face and the constructor
# --------------------------------------------------------------------------


def test_every_file_is_fetched_at_the_pinned_revision(hub, tmp_path):
    Reranker(PINNED_NAME, tmp_path / "cache").rerank("q", ["a"])

    assert {c["file"] for c in hub.calls} == set(CONTENTS)
    assert all(c["revision"] == REVISION for c in hub.calls)
    assert all(c["repo"] == REPO for c in hub.calls)
    # Plain files in a directory Cognita owns under the model cache, never the
    # symlinked HF snapshot (ONNX Runtime rejects external data reached through
    # a symlink into another blob directory; kei, 2026-09-28).
    want = embeddings.pinned_model_dir(
        embeddings.PINNED_RERANKERS[PINNED_NAME], str(tmp_path / "cache")
    )
    assert want == tmp_path / "cache" / "pinned" / REPO.replace("/", "--") / REVISION
    assert all(Path(c["local_dir"]) == want for c in hub.calls)


@pytest.mark.parametrize("reject_session_options,reject_threads,step", [
    (False, False, "arena-off"),
    (True, False, "default"),
    (True, True, "no-threads"),
])
def test_specific_model_path_reaches_the_constructor_on_every_rung(
    hub, tmp_path, reject_session_options, reject_threads, step, caplog
):
    _FakeCrossEncoder.reject_session_options = reject_session_options
    _FakeCrossEncoder.reject_threads = reject_threads

    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        assert Reranker(PINNED_NAME, tmp_path, threads=4).rerank("q", ["a"]) == [1.0]

    kwargs = _FakeCrossEncoder.instances[-1].init_kwargs
    assert kwargs["specific_model_path"] == str(hub.snapshot)
    assert kwargs["model_name"] == PINNED_NAME
    assert ("threads" in kwargs) == (step != "no-threads")
    assert ("extra_session_options" in kwargs) == (step == "arena-off")
    ready = [m for m in _messages(caplog, logging.INFO) if m.startswith("reranker.ready")]
    assert len(ready) == 1 and f"step={step}" in ready[0] and PINNED_NAME in ready[0]


def test_first_fetch_logs_download_start_and_done_and_a_cached_load_logs_neither(
    hub, tmp_path, caplog
):
    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        Reranker(PINNED_NAME, tmp_path).rerank("q", ["a"])
    infos = _messages(caplog, logging.INFO)
    starts = [m for m in infos if m.startswith("reranker.download start")]
    dones = [m for m in infos if m.startswith("reranker.download done")]
    assert len(starts) == 1 and len(dones) == 1
    total = sum(len(d) for d in CONTENTS.values())
    assert f"revision={REVISION}" in starts[0] and f"bytes={total}" in starts[0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        Reranker(PINNED_NAME, tmp_path).rerank("q", ["a"])  # every file is cached now
    infos = _messages(caplog, logging.INFO)
    assert not [m for m in infos if m.startswith("reranker.download")]
    assert [m for m in infos if m.startswith("reranker.verify ok")]


# --------------------------------------------------------------------------
# 3. R9: a corrupt cache is refused, loudly, and nothing is deleted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rel,bad", [
    ("onnx/model.onnx_data", b"weights-" * 63 + b"XXXXXXXX"),  # same size, other bytes
    ("onnx/model.onnx", b"short"),                              # wrong size
])
def test_a_verification_mismatch_fails_the_load_and_deletes_nothing(
    hub, tmp_path, rel, bad, caplog
):
    hub.corrupt[rel] = bad
    reranker = Reranker(PINNED_NAME, tmp_path)

    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        assert reranker.rerank("q", ["a"]) is None
        assert reranker.rerank("q", ["a"]) is None  # sticky: no second attempt

    errors = _messages(caplog, logging.ERROR)
    assert len(errors) == 1 and str(hub.snapshot / rel) in errors[0]
    assert len(_messages(caplog, logging.WARNING)) == 1
    assert reranker.state() == "failed"
    assert (hub.snapshot / rel).read_bytes() == bad  # left exactly as found
    assert _FakeCrossEncoder.instances == []


# --------------------------------------------------------------------------
# 4. R10: background load
# --------------------------------------------------------------------------


def test_background_load_answers_none_immediately_then_scores(hub, tmp_path, caplog):
    entered = threading.Event()
    gate = threading.Event()

    def block_in_constructor():
        entered.set()
        # Hang guard only: `gate` is set by this test in every passing run.
        assert gate.wait(timeout=5), "test never released the fake constructor"

    _FakeCrossEncoder.on_construct = block_in_constructor
    reranker = Reranker(PINNED_NAME, tmp_path)
    assert reranker.state() == "not_loaded"

    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        reranker.start_background_load()
        assert entered.wait(timeout=5), "the load thread never reached the constructor"
        thread = reranker._load_thread
        assert thread is not None and thread.name == "reranker-load" and thread.daemon

        assert reranker.state() == "loading"
        assert reranker.rerank("q", ["a"]) is None
        assert reranker.rerank("q", ["a", "b"]) is None
        # A second start while one is running creates no second thread.
        reranker.start_background_load()
        assert reranker._load_thread is thread
        assert [t for t in threading.enumerate() if t.name == "reranker-load"] == [thread]

        gate.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert reranker.state() == "ready"
    assert reranker.rerank("q", ["a", "b"]) == [1.0, 1.0]
    reranker.start_background_load()  # loaded already: nothing to start
    assert reranker._load_thread is thread

    loading = [m for m in _messages(caplog, logging.INFO) if m.startswith("reranker.loading")]
    assert len(loading) == 1
    assert "RRF order" in loading[0]
    assert len(_FakeCrossEncoder.instances) == 1


# --------------------------------------------------------------------------
# 5. The search cache must not keep an unreranked answer
# --------------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.searches = 0

    async def dense_search(self, project, embedding, limit, category=None):
        self.searches += 1
        return [ChunkHit(
            chunk_id="d_0", doc_id="d", chunk_index=0, content="some content",
            section=None, source="doc.md", category="general", keywords=[], score=0.1,
        )]

    async def lexical_search(self, project, query, limit, category=None):
        return []

    async def registered_lexical_search(self, project, query, limit, category=None):
        return []

    async def adjacent_chunks(self, project, wanted):
        return {}


class _CountingReranker:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def rerank(self, query, texts):
        self.calls += 1
        return None if self.scores is None else [self.scores] * len(texts)


async def test_an_unreranked_answer_is_not_cached():
    reranker = _CountingReranker(None)
    core = RetrievalCore(_Store(), HashEmbedder(), reranker)

    first = await core.search("P", "query", hybrid_alpha=1.0)
    await core.search("P", "query", hybrid_alpha=1.0)

    assert first[0]["reranker_score"] is None
    assert reranker.calls == 2  # the second query reached the reranker again


async def test_a_permanently_failed_reranker_does_not_disable_the_cache():
    """No score will ever arrive, so the RRF answer is final and is cached, as in 13.x."""
    reranker = _CountingReranker(None)
    reranker.state = lambda: "failed"
    core = RetrievalCore(store := _Store(), HashEmbedder(), reranker)

    await core.search("P", "query", hybrid_alpha=1.0)
    await core.search("P", "query", hybrid_alpha=1.0)

    assert store.searches == 1
    assert reranker.calls == 1


async def test_a_reranked_answer_is_served_from_the_cache_the_second_time():
    reranker = _CountingReranker(0.75)
    core = RetrievalCore(_Store(), HashEmbedder(), reranker)

    first = await core.search("P", "query", hybrid_alpha=1.0)
    second = await core.search("P", "query", hybrid_alpha=1.0)

    assert first[0]["reranker_score"] == 0.75
    assert second == first
    assert reranker.calls == 1


async def test_with_no_reranker_configured_results_are_cached_as_before():
    core = RetrievalCore(store := _Store(), HashEmbedder(), None)

    await core.search("P", "query", hybrid_alpha=1.0)
    await core.search("P", "query", hybrid_alpha=1.0)

    assert store.searches == 1


# --------------------------------------------------------------------------
# 6. R10: no background load means today's synchronous lazy load
# --------------------------------------------------------------------------


def test_without_a_background_load_rerank_loads_synchronously(hub, tmp_path):
    reranker = Reranker(PINNED_NAME, tmp_path)
    assert reranker.state() == "not_loaded"

    assert reranker.rerank("q", ["a", "b"]) == [1.0, 1.0]  # loaded inline, not None

    assert reranker.state() == "ready"
    assert reranker._load_thread is None


def _host(tmp_path, reranker):
    registry = Registry(tmp_path / "registry.yaml")
    store = Store("postgresql://nowhere/none", embedding_dimensions=32)
    return LocalEngineHost(
        CognitaConfig(), registry, RetrievalCore(store, HashEmbedder(32), reranker)
    )


def _refuse_connect(host, monkeypatch, order=None):
    async def refuse():
        if order is not None:
            order.append("connect")
        raise SchemaVersionMismatch("schema mismatch for the test")

    monkeypatch.setattr(host.store, "connect", refuse)


async def test_startup_works_with_a_reranker_that_has_no_background_load(
    tmp_path, monkeypatch
):
    from retrieval_fakes import OverlapReranker

    host = _host(tmp_path, OverlapReranker())
    _refuse_connect(host, monkeypatch)
    await host.startup()  # must not raise


async def test_startup_works_with_no_reranker_at_all(tmp_path, monkeypatch):
    host = _host(tmp_path, None)
    _refuse_connect(host, monkeypatch)
    await host.startup()  # must not raise


async def test_startup_starts_the_background_load_before_touching_the_database(
    tmp_path, monkeypatch
):
    order: list[str] = []

    class Loading:
        def start_background_load(self):
            order.append("start_background_load")

        def rerank(self, query, texts):
            return None

    host = _host(tmp_path, Loading())
    _refuse_connect(host, monkeypatch, order)
    await host.startup()  # the schema refusal below must not stop the warm-up

    assert order == ["start_background_load", "connect"]


# --------------------------------------------------------------------------
# 7. R11: an unloadable model is one WARNING and then RRF order
# --------------------------------------------------------------------------


def test_a_failed_download_is_one_warning_then_rrf_order(hub, tmp_path, caplog):
    hub.raises = OSError("no route to host")
    reranker = Reranker(PINNED_NAME, tmp_path)

    with caplog.at_level(logging.INFO, logger="cognita.embeddings"):
        assert reranker.rerank("q", ["a"]) is None
        assert reranker.rerank("q", ["a"]) is None

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1 and "fall back to RRF order" in warnings[0]
    assert reranker.state() == "failed"
    assert len(hub.calls) == 1  # no retry inside the process


# --------------------------------------------------------------------------
# 8. R12: any other name is untouched
# --------------------------------------------------------------------------


def test_a_non_pinned_name_never_touches_the_pinning_machinery(hub, tmp_path):
    name = "jinaai/jina-reranker-v2-base-multilingual"
    assert name not in embeddings.PINNED_RERANKERS

    assert Reranker(name, tmp_path, threads=4).rerank("q", ["a"]) == [1.0]

    assert hub.calls == []
    assert _FakeCrossEncoder.custom_models == []
    assert _FakeCrossEncoder.instances[-1].init_kwargs == {
        "model_name": name,
        "cache_dir": str(tmp_path),
        "threads": 4,
        "extra_session_options": {"enable_cpu_mem_arena": False},
    }


def test_the_shipped_table_pins_bge_reranker_v2_m3():
    spec = embeddings.PINNED_RERANKERS["BAAI/bge-reranker-v2-m3"]
    assert spec.repo == "onnx-community/bge-reranker-v2-m3-ONNX"
    assert spec.revision == "6f5ff65298512715a1e669753bc754d2bc8f367b"
    assert spec.model_file == "onnx/model.onnx"
    assert spec.additional_files == ("onnx/model.onnx_data",)
    assert spec.files["onnx/model.onnx_data"] == (
        2_271_088_656,
        "f009aa6c6cf21986fd7e0021fa66b20ccce27abc6900a57c7109c8496811bcbe",
    )
    assert set(spec.files) == {
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "onnx/model.onnx", "onnx/model.onnx_data",
    }
    assert CognitaConfig().reranker_model == "BAAI/bge-reranker-v2-m3"


# --------------------------------------------------------------------------
# 9. R13: /healthz and get_index_stats report the reranker
# --------------------------------------------------------------------------


def _gateway(tmp_path, engine):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml")
    return config, create_gateway_app(config, registry, engine=engine)


def _engine(reranker):
    return SimpleNamespace(
        make_client=lambda: httpx.AsyncClient(),
        core=SimpleNamespace(reranker=reranker),
    )


async def _healthz(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    return response.json()


async def test_healthz_reports_the_reranker_model_and_state(tmp_path):
    reranker = Reranker("BAAI/bge-reranker-v2-m3", tmp_path)
    config, app = _gateway(tmp_path, _engine(reranker))

    body = await _healthz(app)
    assert body["reranker"] == {"model": config.reranker_model, "state": "not_loaded"}
    assert body["reranker"]["model"] == "BAAI/bge-reranker-v2-m3"

    reranker._load_thread = threading.Thread()  # a load in progress, never started
    assert (await _healthz(app))["reranker"]["state"] == "loading"


async def test_healthz_says_unknown_for_a_reranker_without_state(tmp_path):
    from retrieval_fakes import OverlapReranker

    _, app = _gateway(tmp_path, _engine(OverlapReranker()))
    assert (await _healthz(app))["reranker"]["state"] == "unknown"


@pytest.mark.parametrize("engine", [
    None,
    SimpleNamespace(make_client=lambda: httpx.AsyncClient()),  # no core (FakeEngineHost)
    SimpleNamespace(make_client=lambda: httpx.AsyncClient(), core=SimpleNamespace()),
    SimpleNamespace(make_client=lambda: httpx.AsyncClient(),
                    core=SimpleNamespace(reranker=None)),
])
async def test_healthz_says_disabled_when_any_link_is_missing(tmp_path, engine):
    _, app = _gateway(tmp_path, engine)
    body = await _healthz(app)
    assert body["reranker"] == {"model": "disabled", "state": "disabled"}


async def test_healthz_never_raises_when_state_does(tmp_path):
    class Exploding:
        def state(self):
            raise RuntimeError("boom")

    _, app = _gateway(tmp_path, _engine(Exploding()))
    assert (await _healthz(app))["reranker"] == {"model": "disabled", "state": "disabled"}


async def test_get_index_stats_reports_the_configured_reranker_name(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    project = Project(name="STATS", documents_dir=docs, data_dir=tmp_path / "data")
    registry.add(project)
    store = Store("postgresql://nowhere/none", embedding_dimensions=32)
    core = RetrievalCore(store, HashEmbedder(32), _CountingReranker(0.5))
    host = LocalEngineHost(CognitaConfig(), registry, core)

    async def stats(name):
        return SimpleNamespace(
            documents=0, chunks=0, embedded_documents=0, registered_documents=0
        )

    async def category_counts(name):
        return {}

    monkeypatch.setattr(store, "stats", stats)
    monkeypatch.setattr(store, "category_counts", category_counts)

    payload = await host._get_index_stats(project, {})
    assert payload["stats"]["reranker_model"] == "BAAI/bge-reranker-v2-m3"
