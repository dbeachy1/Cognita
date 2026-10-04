"""Fixtures shared across suites.

``fake_fastembed`` and ``trim_calls`` were local to ``test_embedding_memory.py``
until DESIGN-6.0's telemetry suite needed the same stub. Importing a fixture
from one test module into another shadows it on every use, so they live here
instead, where pytest resolves them by name and neither suite imports the other.
"""

from __future__ import annotations

import sys
import types

import pytest

from cognita import embeddings
from fastembed_fakes import _FakeCrossEncoder, _FakeModelSource, _FakeTextEmbedding


@pytest.fixture
def full_mode_workspace_service():
    """Mark a gateway fixture as a full-mode host with Workspace configured.

    Gateway unit tests run in the app-test container, separate from the
    throwaway Core or Full service container. They must select host capability
    explicitly instead of inheriting the release stack's runtime mode. Tests
    that exercise Workspace calls should provide a functional service fake;
    this sentinel is for catalog and self-test-plan coverage only.
    """
    return object()


@pytest.fixture
def fake_fastembed(monkeypatch):
    """Install a fastembed stub so no model is downloaded or loaded."""
    _FakeTextEmbedding.instances = []
    _FakeTextEmbedding.reject_session_options = False
    _FakeCrossEncoder.instances = []
    _FakeCrossEncoder.reject_threads = False
    _FakeCrossEncoder.reject_session_options = False
    _FakeCrossEncoder.built_in_models = ["Xenova/ms-marco-MiniLM-L-6-v2"]
    _FakeCrossEncoder.custom_models = []
    _FakeCrossEncoder.on_construct = None

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = _FakeTextEmbedding
    rerank_pkg = types.ModuleType("fastembed.rerank")
    cross = types.ModuleType("fastembed.rerank.cross_encoder")
    cross.TextCrossEncoder = _FakeCrossEncoder
    # 14.0 §2.3: `_register_pinned` imports ModelSource from here.
    common_pkg = types.ModuleType("fastembed.common")
    model_description = types.ModuleType("fastembed.common.model_description")
    model_description.ModelSource = _FakeModelSource

    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross)
    monkeypatch.setitem(sys.modules, "fastembed.common", common_pkg)
    monkeypatch.setitem(sys.modules, "fastembed.common.model_description", model_description)
    # Pin the core count: thread counts are clamped to it, so without this these
    # assertions quietly depend on whatever machine runs them.
    monkeypatch.setattr(embeddings.os, "cpu_count", lambda: 32)
    return fastembed


@pytest.fixture
def trim_calls(monkeypatch):
    """Count release_to_os() calls as the modules under test see them."""
    calls = []
    monkeypatch.setattr(embeddings, "release_to_os", lambda: calls.append(1))
    return calls
