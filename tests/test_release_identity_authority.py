"""13.0 §4: `release_identity` is the ONE place a version or generation lives.

The failure this pins is the one the repo keeps having: two spellings of the
same number that drift, so `/healthz` and the wheel metadata answer differently
and "what is actually running on the box?" needs an SSH and a SHA diff. Every
assertion below compares a surface a human or a script actually reads against
the authority — never against another copy of the literal.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient

import cognita
from cognita import connectors as connectors_mod
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import (
    ConnectorStore,
    build_connector_path,
    build_connector_url,
)
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.release_identity import (
    APPLICATION_VERSION,
    COMBINED_CONTRACT_VERSION,
    DATABASE_SCHEMA_VERSION,
    IDENTITY_SCHEMA,
    TOOLBOX_VERSION,
    WORKSPACE_CONTRACT_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_FILE = ROOT / "src" / "cognita" / "release_identity.py"


# --- the module is a leaf ---------------------------------------------------


def test_the_authority_imports_nothing_from_the_package():
    """A leaf, checked in the source rather than by import order.

    It matters because everything else imports IT: a build backend reads the
    version by attribute, and `cognita/__init__` derives `__version__` from it.
    One package import here would make both of those cost the whole
    application, and would open a cycle through `__init__`.
    """
    tree = ast.parse(IDENTITY_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from __future__ import annotations` is the one allowed import.
            assert node.level == 0 and node.module == "__future__", ast.dump(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("cognita"), alias.name


def test_importing_the_authority_pulls_in_nothing_else():
    """Proven in a fresh interpreter, because this suite has already imported
    half the package by the time it runs."""
    code = (
        "import sys, json\n"
        "import cognita.release_identity as ri\n"
        "print(json.dumps({\n"
        "    'modules': sorted(m for m in sys.modules if m.startswith('cognita')),\n"
        "    'version': ri.APPLICATION_VERSION,\n"
        "}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        cwd=str(ROOT), timeout=120,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # `cognita` itself is unavoidable: importing a submodule runs the package
    # __init__, which derives __version__ from this very module.
    assert payload["modules"] == ["cognita", "cognita.release_identity"]
    assert payload["version"] == APPLICATION_VERSION


def test_the_identity_names_are_the_ones_the_design_declares():
    assert IDENTITY_SCHEMA == 1
    assert APPLICATION_VERSION == "16.0.0"
    assert COMBINED_CONTRACT_VERSION == 6
    assert WORKSPACE_CONTRACT_VERSION == 3
    assert DATABASE_SCHEMA_VERSION == 1
    assert TOOLBOX_VERSION == "12.6.0"
    assert isinstance(WORKSPACE_CONTRACT_VERSION, int) and WORKSPACE_CONTRACT_VERSION >= 1
    assert isinstance(DATABASE_SCHEMA_VERSION, int) and DATABASE_SCHEMA_VERSION >= 1
    assert isinstance(TOOLBOX_VERSION, str)


# --- everything that reports a version derives from it ----------------------


def test_the_package_version_is_the_authority():
    assert cognita.__version__ == APPLICATION_VERSION
    source = (ROOT / "src" / "cognita" / "__init__.py").read_text(encoding="utf-8")
    assert "APPLICATION_VERSION as __version__" in source
    assert f'__version__ = "{APPLICATION_VERSION}"' not in source


def test_the_installed_metadata_version_is_the_authority():
    """The wheel/editable metadata, which is what `pip show` and a container
    label report. A stale value here means the build was made before the bump
    — exactly the "did the fix land?" ambiguity 13.0 §4 exists to remove.

    Every discoverable distribution is checked, not just the first one
    `version()` happens to return. A tree can carry build residue — a stale
    `src/cognita.egg-info` from an older install sits on `sys.path` beside the
    real dist-info, and which of the two answers depends on path order, so the
    same interpreter can report two different versions in one session. That is
    exactly the ambiguity this file exists to prevent, so it fails here, with
    the paths named, rather than being tolerated.
    """
    found = {
        dist.version: str(getattr(dist, "_path", dist))
        for dist in importlib.metadata.distributions()
        if (dist.metadata["Name"] or "").lower() == "cognita"
    }
    assert found == {APPLICATION_VERSION: found.get(APPLICATION_VERSION, "")}, found
    assert importlib.metadata.version("cognita") == APPLICATION_VERSION


def test_pyproject_reads_the_version_from_the_authority():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "cognita.release_identity.APPLICATION_VERSION" }' in pyproject
    assert f'version = "{APPLICATION_VERSION}"' not in pyproject


def test_the_broker_reports_the_authority_version():
    source = (ROOT / "src" / "cognita" / "runtime_broker" / "app.py").read_text(encoding="utf-8")
    assert "version=APPLICATION_VERSION," in source
    assert f'version="{APPLICATION_VERSION}"' not in source


# --- the served surfaces ----------------------------------------------------


def _app(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path / "d"))
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    key = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Primary", project_names=["KEI"]
    ).connectors[0]
    config = CognitaConfig(
        registry_path=registry.path, connectors_path=connectors.path,
        data_root=tmp_path, public_base_url="https://cognita.example",
    )
    app = create_gateway_app(
        config, registry, connector_store=connectors, authentication_store=auth,
    )
    return app, connector.slug, key


async def test_healthz_reports_the_authority_version(tmp_path):
    app, _slug, _key = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://cognita.example") as client:
        payload = (await client.get("/healthz")).json()
    assert payload["version"] == APPLICATION_VERSION


async def test_initialize_server_info_reports_the_authority_version(tmp_path):
    app, slug, key = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://cognita.example") as client:
        response = await client.post(
            f"/mcp/connectors/{slug}/mcp/v{COMBINED_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": f"Bearer {key}"},
        )
    server_info = response.json()["result"]["serverInfo"]
    assert server_info["version"] == APPLICATION_VERSION
    # The icon URL is cache-busted with the same value; a second spelling here
    # would send clients a stale asset after an upgrade.
    assert f"?v={APPLICATION_VERSION}" in server_info["icons"][0]["src"]


async def test_the_self_test_plan_reports_the_authority_version(tmp_path):
    app, slug, key = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://cognita.example") as client:
        response = await client.post(
            f"/mcp/connectors/{slug}/mcp/v{COMBINED_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "get_self_test_plan", "arguments": {"project": "KEI"},
            }},
            headers={"Authorization": f"Bearer {key}"},
        )
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["server_version"] == APPLICATION_VERSION
    # The plan quotes the canonical raw-transport URL; its generation is the
    # authority's, not prose.
    assert (
        f"/mcp/connectors/<connector-slug>/mcp/v{COMBINED_CONTRACT_VERSION}"
        in payload["plan"]
    )


# --- the contract generations -----------------------------------------------


def test_the_connector_module_re_exports_the_authority():
    assert connectors_mod.PUBLIC_CONTRACT_VERSION == COMBINED_CONTRACT_VERSION
    assert connectors_mod.WORKSPACE_CONTRACT_VERSION == WORKSPACE_CONTRACT_VERSION
    assert connectors_mod.PREVIOUS_CONTRACT_VERSION == COMBINED_CONTRACT_VERSION - 1
    source = (ROOT / "src" / "cognita" / "connectors.py").read_text(encoding="utf-8")
    assert "PUBLIC_CONTRACT_VERSION = 5" not in source
    assert "WORKSPACE_CONTRACT_VERSION = 3" not in source


def test_the_route_builders_use_the_authority_generation():
    assert build_connector_path("primary") == (
        f"/mcp/connectors/primary/mcp/v{COMBINED_CONTRACT_VERSION}"
    )
    assert build_connector_url("https://cognita.example", "primary") == (
        f"https://cognita.example/mcp/connectors/primary/mcp/v{COMBINED_CONTRACT_VERSION}"
    )
