"""14.0.0: the 3.x worker engine, stdio mode and knowledge-rag are gone.

DESIGN-14.0-REPLACE-LICENSED-COMPONENTS §3 and §8 "knowledge-rag removal". The
point of these tests is the failure mode CLAUDE.md keeps warning about: removing
a config field alone would make an old `engine: workers` config start quietly as
core, so the refusal has to be loud and has to name where the value came from.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig, load_config
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry

SRC = Path(__file__).resolve().parents[1] / "src" / "cognita"


def _config_file(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "cognita.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_engine_env(monkeypatch):
    monkeypatch.delenv("COGNITA_ENGINE", raising=False)


def test_engine_workers_in_the_config_file_is_refused_naming_the_file(tmp_path):
    path = _config_file(tmp_path, {"engine": "workers"})
    with pytest.raises(SystemExit) as raised:
        load_config(path)
    message = str(raised.value)
    assert str(path) in message
    assert "engine: workers" in message
    assert "14.0.0" in message
    assert "Delete the engine line" in message


def test_engine_workers_in_the_environment_is_refused_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNITA_ENGINE", "workers")
    with pytest.raises(SystemExit) as raised:
        load_config(_config_file(tmp_path, {}))
    message = str(raised.value)
    assert "COGNITA_ENGINE" in message
    assert "workers" in message
    assert "14.0.0" in message


def test_engine_workers_is_refused_even_with_no_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNITA_ENGINE", "workers")
    with pytest.raises(SystemExit):
        load_config(tmp_path / "does-not-exist.yaml")


def test_any_other_engine_value_is_refused_too(tmp_path):
    with pytest.raises(SystemExit) as raised:
        load_config(_config_file(tmp_path, {"engine": "chroma"}))
    assert "engine: chroma is no longer supported" in str(raised.value)


@pytest.mark.parametrize("data", [{}, {"engine": "core"}, {"engine": None}])
def test_core_or_no_engine_key_loads(tmp_path, data):
    config = load_config(_config_file(tmp_path, data))
    assert isinstance(config, CognitaConfig)
    # The field is gone; nothing may keep reading it.
    assert not hasattr(config, "engine")


def test_engine_core_in_the_environment_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNITA_ENGINE", "core")
    assert isinstance(load_config(_config_file(tmp_path, {})), CognitaConfig)


def test_the_removed_worker_and_stdio_keys_are_ignored_not_fatal(tmp_path):
    config = load_config(_config_file(tmp_path, {
        "worker_port_min": 8677, "worker_port_max": 8777,
        "worker_probe_interval_s": 60, "stdio_default_project": "x",
    }))
    for gone in ("worker_port_min", "worker_port_max", "worker_probe_interval_s",
                 "stdio_default_project"):
        assert not hasattr(config, gone)


def test_models_cache_dir_default_is_portable(tmp_path, monkeypatch):
    # Carried over from the deleted tests/test_stdio_mode.py, whose config
    # tests covered this live setting alongside the stdio one.
    monkeypatch.delenv("COGNITA_MODELS_CACHE_DIR", raising=False)
    config = load_config(tmp_path / "missing.yaml")
    assert config.models_cache_dir == Path.home() / ".cache" / "cognita" / "models"


def test_models_cache_dir_yaml_then_environment_precedence(tmp_path, monkeypatch):
    # Carried over from the deleted tests/test_stdio_mode.py (see above).
    yaml_cache = tmp_path / "yaml-models"
    env_cache = tmp_path / "env-models"
    path = _config_file(tmp_path, {"models_cache_dir": str(yaml_cache)})
    monkeypatch.delenv("COGNITA_MODELS_CACHE_DIR", raising=False)
    assert load_config(path).models_cache_dir == yaml_cache
    monkeypatch.setenv("COGNITA_MODELS_CACHE_DIR", str(env_cache))
    assert load_config(path).models_cache_dir == env_cache


def test_the_oauth_port_may_now_sit_where_the_worker_range_was():
    # The 8677-8777 range was reserved for worker processes; nothing listens
    # there any more, so the validator no longer refuses a port inside it.
    assert CognitaConfig(oauth_service_port=8700).oauth_service_port == 8700


def test_serve_stdio_is_refused_with_the_plain_message(tmp_path, monkeypatch):
    from cognita.__main__ import cmd_serve

    monkeypatch.setenv("COGNITA_CONFIG_PATH", str(_config_file(tmp_path, {})))
    args = argparse.Namespace(stdio=True, project="x", test=False,
                              revoke_oauth_tokens_on_start=False)
    with pytest.raises(SystemExit) as raised:
        cmd_serve(args)
    message = str(raised.value)
    assert "stdio mode was removed in Cognita 14.0.0" in message
    assert "HTTP to /mcp" in message


@pytest.mark.parametrize("name", ["workers", "stdio_mode", "worker_client"])
def test_the_removed_modules_do_not_import(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"cognita.{name}")


def test_no_source_file_imports_the_removed_engine_or_the_mcp_client():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("import knowledge_rag", "from knowledge_rag", "from mcp ",
                       "import mcp", "from .workers", "from .worker_client",
                       "from .stdio_mode", "WorkerSupervisor"):
            if needle in text:
                offenders.append((path.name, needle))
    assert offenders == []


def test_worker_status_values_are_the_admin_wire_values():
    from cognita.admin_api import WorkerStatus

    assert {status.name: status.value for status in WorkerStatus} == {
        "STOPPED": "stopped", "STARTING": "starting", "RUNNING": "running", "ERROR": "error",
    }


@pytest.mark.asyncio
async def test_a_gateway_with_no_engine_answers_project_unavailable(tmp_path):
    """The replacement for the old "no supervisor / worker not running" answer."""
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = store.create(expected_revision=0, name="Primary", project_names=["KEI"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path),
        registry, connector_store=store, authentication_store=auth,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/mcp/connectors/{connector.connectors[0].slug}/mcp/v5",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "list_categories", "arguments": {"project": "KEI"}}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["reason"] == "project_unavailable"
