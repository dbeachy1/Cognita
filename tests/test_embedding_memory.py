"""5.8: embedding memory is returned to the OS instead of held forever.

Three separate mechanisms, tested separately because they fail separately:

1. ``release_to_os()`` — a glibc ``malloc_trim(0)`` that must degrade to a
   silent no-op anywhere libc.so.6 is absent (every Windows dev box, and the
   test runner itself).
2. The ORT intra-op thread cap, which is what actually bounds the arena. It is
   passed as a kwarg only when non-zero, because ``threads=0`` is the documented
   escape hatch back to the pre-5.8.0 unbounded behavior.
3. Streaming + batching in ``embed()``, which must change *when* vectors are
   produced and never *what* they are (D4.8 parity), and must not put a trim on
   the single-query search path — that one runs per user query and stays hot.

No model is ever downloaded here: fastembed is replaced in ``sys.modules`` with
a recording stub (``fastembed_fakes``, installed by the ``fake_fastembed``
fixture in ``conftest.py``), so these assert on the call the wrapper makes.
"""

import sys
import types

import pytest

from cognita import embeddings
from cognita.embeddings import Embedder, EmbeddingUnavailable, Reranker, release_to_os
from fastembed_fakes import _FakeCrossEncoder, _FakeTextEmbedding, _FakeVector


def _embedder(tmp_path, **kw):
    return Embedder("fake-model", 4, tmp_path, **kw)


# --------------------------------------------------------------------------
# 1. release_to_os()
# --------------------------------------------------------------------------


def test_release_to_os_never_raises_without_glibc(monkeypatch):
    """No libc.so.6 (Windows, musl) must be a silent no-op, never an error."""
    import ctypes

    monkeypatch.setattr(embeddings, "_LIBC", None)
    monkeypatch.setattr(
        ctypes, "CDLL", lambda name: (_ for _ in ()).throw(OSError("no libc.so.6"))
    )
    release_to_os()  # must not raise


def test_release_to_os_never_raises_when_trim_is_missing(monkeypatch):
    """A libc without malloc_trim must not take the process down either."""

    class _NoTrim:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(embeddings, "_LIBC", _NoTrim())
    release_to_os()  # must not raise


def test_release_to_os_calls_malloc_trim_when_available(monkeypatch):
    """On glibc it must actually reach malloc_trim(0) — the whole point."""
    seen = []

    class _Libc:
        def malloc_trim(self, arg):
            seen.append(arg)
            return 1

    monkeypatch.setattr(embeddings, "_LIBC", _Libc())
    release_to_os()
    assert seen == [0]


def test_release_to_os_is_importable_from_retrieval():
    """retrieval.py trims after a whole-corpus walk; the symbol must be there."""
    from cognita.retrieval import release_to_os as from_retrieval

    assert from_retrieval is embeddings.release_to_os


@pytest.mark.asyncio
async def test_index_project_trims_when_the_walk_finishes(tmp_path, monkeypatch):
    """The post-walk trim is the one the handoff asked for; assert it FIRES.

    Importing the symbol is not evidence that it is called — this drives
    index_project through a stub and watches for the call.
    """
    from cognita import retrieval

    calls = []
    monkeypatch.setattr(retrieval, "release_to_os", lambda: calls.append(1))

    core = retrieval.RetrievalCore.__new__(retrieval.RetrievalCore)

    async def _fake_locked(
        project, documents_dir, *, force=False, progress=None, job=None,
        before_removal=None,
    ):
        assert calls == [], "the trim must run AFTER the walk, not before"
        return {"indexed": 1}

    monkeypatch.setattr(core, "_index_project_locked", _fake_locked)
    result = await core.index_project("P", tmp_path)

    assert result == {"indexed": 1}
    assert calls == [1]


@pytest.mark.asyncio
async def test_index_project_trims_even_when_the_walk_raises(tmp_path, monkeypatch):
    """A failed rebuild has still allocated; the pages must come back anyway."""
    from cognita import retrieval

    calls = []
    monkeypatch.setattr(retrieval, "release_to_os", lambda: calls.append(1))

    core = retrieval.RetrievalCore.__new__(retrieval.RetrievalCore)

    async def _boom(
        project, documents_dir, *, force=False, progress=None, job=None,
        before_removal=None,
    ):
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(core, "_index_project_locked", _boom)
    with pytest.raises(RuntimeError, match="walk exploded"):
        await core.index_project("P", tmp_path)

    assert calls == [1]


# --------------------------------------------------------------------------
# 2. The ORT thread cap
# --------------------------------------------------------------------------


def test_embedder_passes_threads_when_non_zero(fake_fastembed, tmp_path):
    _embedder(tmp_path, threads=4)._ensure_model()
    assert _FakeTextEmbedding.instances[0].init_kwargs["threads"] == 4


def test_embedder_omits_threads_when_zero(fake_fastembed, tmp_path):
    """threads=0 is the documented escape hatch to unbounded ORT sizing."""
    _embedder(tmp_path, threads=0)._ensure_model()
    assert "threads" not in _FakeTextEmbedding.instances[0].init_kwargs


def test_embedder_keeps_cpu_provider_and_cache_dir(fake_fastembed, tmp_path):
    """The thread cap must not disturb the existing construction contract."""
    kwargs = _embedder(tmp_path, threads=4)._ensure_model().init_kwargs
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    assert kwargs["model_name"] == "fake-model"
    assert kwargs["cache_dir"] == str(tmp_path)


def test_reranker_passes_threads_when_non_zero(fake_fastembed, tmp_path):
    Reranker("fake-reranker", tmp_path, threads=4)._ensure_model()
    assert _FakeCrossEncoder.instances[0].init_kwargs["threads"] == 4


def test_reranker_omits_threads_when_zero(fake_fastembed, tmp_path):
    Reranker("fake-reranker", tmp_path, threads=0)._ensure_model()
    assert "threads" not in _FakeCrossEncoder.instances[0].init_kwargs


def test_reranker_retries_without_threads_if_rejected(fake_fastembed, tmp_path):
    """A fastembed build without `threads` must still LOAD the reranker.

    Losing the arena cap here is much cheaper than losing the reranker: without
    it, search silently degrades to RRF order.
    """
    _FakeCrossEncoder.reject_threads = True
    reranker = Reranker("fake-reranker", tmp_path, threads=4)
    model = reranker._ensure_model()
    assert model is not None, "reranker must not degrade to RRF over a kwarg"
    assert "threads" not in model.init_kwargs
    assert reranker.rerank("q", ["a", "b"]) == [1.0, 1.0]


def test_reranker_still_degrades_gracefully_on_a_real_load_failure(tmp_path, monkeypatch):
    """The TypeError retry must not swallow a genuine load failure."""
    broken = types.ModuleType("fastembed.rerank.cross_encoder")

    class _Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("model file corrupt")

    broken.TextCrossEncoder = _Broken
    monkeypatch.setitem(sys.modules, "fastembed.rerank", types.ModuleType("fastembed.rerank"))
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", broken)

    reranker = Reranker("fake-reranker", tmp_path, threads=4)
    assert reranker._ensure_model() is None
    assert reranker.rerank("q", ["a"]) is None


# --------------------------------------------------------------------------
# 3. Streaming, batching, and where the trim fires
# --------------------------------------------------------------------------


def test_embed_empty_short_circuits_without_loading_the_model(fake_fastembed, tmp_path):
    assert _embedder(tmp_path).embed([]) == []
    assert _FakeTextEmbedding.instances == [], "empty embed() must not load a model"


def test_embed_passes_the_batch_size_through(fake_fastembed, tmp_path):
    """The escape hatch still has to WORK on a constrained box, so an explicit
    small batch must reach fastembed."""
    _embedder(tmp_path, batch_size=32).embed(["a", "b", "c"])
    assert _FakeTextEmbedding.instances[0].embed_calls[0]["kwargs"]["batch_size"] == 32


def test_embed_uses_fastembeds_own_batch_size_by_default(fake_fastembed, tmp_path):
    """Default must be no throttle: fastembed's 256, not 5.8.0's 32."""
    _embedder(tmp_path).embed(["a", "b", "c"])
    assert _FakeTextEmbedding.instances[0].embed_calls[0]["kwargs"]["batch_size"] == 256


def test_embed_returns_the_same_values_in_the_same_order(fake_fastembed, tmp_path):
    """D4.8 parity: streaming changes WHEN vectors are produced, never WHAT."""
    vectors = _embedder(tmp_path).embed(["a", "b", "c"])
    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]


def test_embed_consumes_a_generator_lazily(fake_fastembed, tmp_path):
    """A list comprehension over the generator would hold every vector at once."""
    seen = []

    class _Watched(_FakeTextEmbedding):
        def embed(self, texts, **kwargs):
            self.embed_calls.append({"texts": list(texts), "kwargs": dict(kwargs)})
            for i, _ in enumerate(texts):
                seen.append(i)
                yield _FakeVector([float(i)] * 4)

    fake_fastembed.TextEmbedding = _Watched
    assert _embedder(tmp_path).embed(["a", "b"]) == [[0.0] * 4, [1.0] * 4]
    assert seen == [0, 1]


def test_bulk_embed_trims(fake_fastembed, tmp_path, trim_calls):
    """A bulk embed is exactly when the arena ratchets up."""
    _embedder(tmp_path).embed(["a", "b"])
    assert trim_calls == [1]


def test_single_query_embed_does_not_trim(fake_fastembed, tmp_path, trim_calls):
    """retrieval.py's query path runs per user query and must stay hot."""
    _embedder(tmp_path).embed(["just the query"])
    assert trim_calls == []


def test_embed_trims_even_when_the_model_raises(fake_fastembed, tmp_path, trim_calls):
    """A failed bulk embed has still allocated; the pages must come back."""

    class _Exploding(_FakeTextEmbedding):
        def embed(self, texts, **kwargs):
            raise RuntimeError("onnx blew up")
            yield  # pragma: no cover - makes this a generator function

    fake_fastembed.TextEmbedding = _Exploding
    with pytest.raises(EmbeddingUnavailable):
        _embedder(tmp_path).embed(["a", "b"])
    assert trim_calls == [1]


def test_embed_still_rejects_a_count_mismatch(fake_fastembed, tmp_path):
    """The 3.8.1 lesson — failures stay LOUD — survives the rewrite."""

    class _Short(_FakeTextEmbedding):
        def embed(self, texts, **kwargs):
            yield _FakeVector([0.0] * 4)

    fake_fastembed.TextEmbedding = _Short
    with pytest.raises(EmbeddingUnavailable, match="count mismatch"):
        _embedder(tmp_path).embed(["a", "b"])


def test_embed_still_rejects_a_dimension_mismatch(fake_fastembed, tmp_path):
    class _Wide(_FakeTextEmbedding):
        def embed(self, texts, **kwargs):
            for _ in texts:
                yield _FakeVector([0.0] * 99)

    fake_fastembed.TextEmbedding = _Wide
    with pytest.raises(EmbeddingUnavailable, match="dim mismatch"):
        _embedder(tmp_path).embed(["a", "b"])


# --------------------------------------------------------------------------
# 4. Defaults and wiring
# --------------------------------------------------------------------------


def test_the_shipped_defaults_throttle_NOTHING():
    """🔴 The regression guard that 5.8.0 needed and did not have.

    Indexing is allowed to take the memory it needs; the requirement is that it
    gives it back afterwards. 5.8.0 inverted that — it capped intra-op threads
    at 4 and the embed batch at 32 so the process could never get big — which
    cost 2.3x rebuild throughput and bought nothing (peak RSS is flat within
    60 MB from 4 threads to unbounded).

    Both keys exist ONLY as escape hatches for a constrained box. The shipped
    defaults must stay identical to using no configuration at all: ORT sizing
    its own pool, and fastembed's own batch size.
    """
    from cognita.config import CognitaConfig

    config = CognitaConfig()
    assert config.embedding_threads == 0, "0 means ORT decides — do not cap the box"
    assert config.embed_batch_size == 256, "fastembed's own default — do not shrink it"


def test_embedder_defaults_match_the_config_defaults(tmp_path, monkeypatch):
    """A directly-constructed Embedder must not disagree with what the server
    builds — a throttle reintroduced in one place and not the other is exactly
    the kind of drift nobody notices until a rebuild is mysteriously slow."""
    from cognita.config import CognitaConfig

    monkeypatch.setattr(embeddings.os, "cpu_count", lambda: 32)
    config = CognitaConfig()
    embedder = _embedder(tmp_path)
    assert embedder._threads == embeddings.resolve_threads(config.embedding_threads)
    assert embedder._batch_size == config.embed_batch_size
    assert Reranker("m", tmp_path)._threads == embeddings.resolve_threads(
        config.embedding_threads
    )


# --------------------------------------------------------------------------
# 5. The caps are escape hatches only, and they are clamped to the box
# --------------------------------------------------------------------------


def test_the_default_embedder_passes_no_thread_cap_to_ort(fake_fastembed, tmp_path):
    """Out of the box, ORT must size its own pool — no `threads` kwarg at all.

    Asserting on the config value alone would not catch a default that gets
    re-throttled at the construction site, which is where it actually bites.
    """
    _embedder(tmp_path)._ensure_model()
    assert "threads" not in _FakeTextEmbedding.instances[0].init_kwargs


def test_the_default_reranker_passes_no_thread_cap_to_ort(fake_fastembed, tmp_path):
    Reranker("fake-reranker", tmp_path)._ensure_model()
    assert "threads" not in _FakeCrossEncoder.instances[0].init_kwargs


def test_both_models_share_the_one_measured_key(fake_fastembed, tmp_path):
    """An earlier draft split these on a CONTENDED measurement suggesting the
    reranker wanted fewer threads. Re-measured clean, both peak at the same
    value, so there is one key and both models are wired to it."""
    import inspect

    from cognita import __main__

    source = inspect.getsource(__main__._build_engine_host)
    assert source.count("config.embedding_threads") == 2
    assert "reranker_threads" not in source


@pytest.mark.parametrize(
    "configured, cpus, expected",
    [
        (16, 32, 16),  # kei: the tuned value survives
        (16, 4, 4),    # a small VM: clamped, never oversubscribed
        (4, 32, 4),    # under the core count: untouched
        (0, 32, 0),    # the escape hatch is a sentinel, not a count
        (-1, 32, 0),   # nonsense normalizes to the sentinel
    ],
)
def test_resolve_threads_clamps_to_the_box(monkeypatch, configured, cpus, expected):
    monkeypatch.setattr(embeddings.os, "cpu_count", lambda: cpus)
    assert embeddings.resolve_threads(configured) == expected


def test_resolve_threads_survives_an_unknown_cpu_count(monkeypatch):
    """os.cpu_count() returns None on some platforms; trust the config then."""
    monkeypatch.setattr(embeddings.os, "cpu_count", lambda: None)
    assert embeddings.resolve_threads(16) == 16


def test_the_clamp_reaches_the_model_construction(fake_fastembed, tmp_path, monkeypatch):
    """Clamping the attribute is pointless if the kwarg is built from the raw value."""
    monkeypatch.setattr(embeddings.os, "cpu_count", lambda: 2)
    _embedder(tmp_path, threads=16)._ensure_model()
    assert _FakeTextEmbedding.instances[0].init_kwargs["threads"] == 2


# --------------------------------------------------------------------------
# 6. The arena is switched OFF so memory actually goes back (5.10.0)
# --------------------------------------------------------------------------


def test_embedder_disables_the_ort_cpu_arena(fake_fastembed, tmp_path):
    """🔴 THE actual fix. ORT's arena keeps its blocks on a private free list
    for the life of the process — that is where the 12.8 GiB lived, and it is
    why malloc_trim reclaimed ~20 MB of a 5.25 GB process. Turning the arena
    off is what makes the memory returnable."""
    _embedder(tmp_path)._ensure_model()
    opts = _FakeTextEmbedding.instances[0].init_kwargs["extra_session_options"]
    assert opts == {"enable_cpu_mem_arena": False}


def test_reranker_disables_the_ort_cpu_arena(fake_fastembed, tmp_path):
    Reranker("fake-reranker", tmp_path)._ensure_model()
    opts = _FakeCrossEncoder.instances[0].init_kwargs["extra_session_options"]
    assert opts == {"enable_cpu_mem_arena": False}


def test_a_build_that_rejects_session_options_still_loads(fake_fastembed, tmp_path):
    """Older fastembed: no `extra_session_options` parameter at all (TypeError).

    A model that fails to load is far worse than one that keeps its arena.
    """
    _FakeTextEmbedding.reject_session_options = True
    model = _embedder(tmp_path)._ensure_model()
    assert model is not None
    assert "extra_session_options" not in model.init_kwargs


def test_a_build_without_the_option_on_its_allowlist_still_loads(fake_fastembed, tmp_path):
    """fastembed asserts when an option is not in EXPOSED_SESSION_OPTIONS.

    Without this fallback the reranker would fail to load and search would
    silently degrade to RRF order — the exact regression 5.8.0 guarded against.
    """
    _FakeCrossEncoder.reject_session_options = True
    reranker = Reranker("fake-reranker", tmp_path)
    model = reranker._ensure_model()
    assert model is not None, "must not degrade to RRF over a session option"
    assert "extra_session_options" not in model.init_kwargs
    assert reranker.rerank("q", ["a", "b"]) == [1.0, 1.0]


def test_disabling_the_arena_is_not_a_cap(fake_fastembed, tmp_path):
    """Guards the distinction Doug drew: stop HOARDING, never LIMIT.

    An index may take whatever memory it needs. Nothing here may quietly
    reintroduce a thread or batch ceiling alongside the arena setting.
    """
    _embedder(tmp_path)._ensure_model()
    kwargs = _FakeTextEmbedding.instances[0].init_kwargs
    assert kwargs["extra_session_options"] == {"enable_cpu_mem_arena": False}
    assert "threads" not in kwargs
