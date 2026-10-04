"""Recording stubs for fastembed — no model is ever downloaded or loaded.

Companion to ``retrieval_fakes.py``: same idea, one layer lower. Those fake the
*embedder*; these fake the library underneath it, so tests can assert on the
call ``cognita.embeddings`` actually makes — which providers it asked for, which
session options, whether it streamed the generator or materialized it.

Shared by ``test_embedding_memory.py`` (5.8/5.10 memory behavior) and
``test_embed_telemetry.py`` (DESIGN-6.0 §14 instrumentation), which assert on
the same construction path from opposite ends. They live here rather than in one
of those suites so neither has to import the other, and in a plain module rather
than ``conftest.py`` because they are classes to instantiate, not fixtures.
"""

from __future__ import annotations


class _FakeVector:
    """Stands in for a numpy array: records that tolist() was the last touch."""

    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


class _FakeTextEmbedding:
    """Records construction kwargs and how embed() was called."""

    instances: list["_FakeTextEmbedding"] = []
    reject_session_options = False

    def __init__(self, **kwargs):
        if type(self).reject_session_options and "extra_session_options" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_session_options'")
        self.init_kwargs = kwargs
        self.embed_calls: list[dict] = []
        type(self).instances.append(self)

    def embed(self, texts, **kwargs):
        self.embed_calls.append({"texts": list(texts), "kwargs": dict(kwargs)})
        for i, text in enumerate(texts):
            # A generator, deliberately: the wrapper must consume it lazily.
            yield _FakeVector([float(i)] * 4)


class _FakeModelSource:
    """Stands in for ``fastembed.common.model_description.ModelSource``."""

    def __init__(self, hf=None, url=None):
        self.hf = hf
        self.url = url


class _FakeCrossEncoder:
    instances: list["_FakeCrossEncoder"] = []
    reject_threads = False
    reject_session_options = False
    # 14.0 §2.3: fastembed's list_supported_models() returns a list of DICTS with
    # a "model" key. A fake that returned bare names would hide a `name in list`
    # check, which is always false against the real thing.
    built_in_models: list[str] = ["Xenova/ms-marco-MiniLM-L-6-v2"]
    # Every add_custom_model call, recorded as its kwargs plus "model".
    custom_models: list[dict] = []
    # Optional hook run at the top of __init__ (tests block the constructor on an
    # Event to hold a background load "in progress").
    on_construct = None

    @classmethod
    def list_supported_models(cls) -> list[dict]:
        supported = [{"model": name} for name in cls.built_in_models]
        supported.extend({"model": spec["model"]} for spec in cls.custom_models)
        return supported

    @classmethod
    def add_custom_model(cls, model, sources, **kwargs):
        cls.custom_models.append({"model": model, "sources": sources, **kwargs})

    def __init__(self, **kwargs):
        if type(self).on_construct is not None:
            type(self).on_construct()
        if type(self).reject_session_options and "extra_session_options" in kwargs:
            raise AssertionError("enable_cpu_mem_arena is unknown or not exposed")
        if type(self).reject_threads and "threads" in kwargs:
            raise TypeError("__init__() got an unexpected keyword argument 'threads'")
        self.init_kwargs = kwargs
        type(self).instances.append(self)

    def rerank(self, query, texts):
        return [1.0 for _ in texts]
