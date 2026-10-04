"""The operator documentation shows the 13.0 commands, and only those.

This replaces `tests/test_release_docs_12.py`, whose live assertions about
README.md, DEPLOYMENT.md, the env template and the version authority are kept
here against the 13.0 docs.  Its 12.x-only assertions (the external release
manifest path in the env file, the per-profile systemd unit files, the
`host-to-container` migration vocabulary) went with the machinery 13.0 section
8 deleted.

A stale command in an operator document is worse than a missing one: it looks
like an instruction and it cannot work.  So the deleted names are allowed in
prose that explains what replaced them, and refused anywhere a reader would
copy from -- a fenced or indented code block.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path

from cognita import __version__
from cognita.release_identity import APPLICATION_VERSION

ROOT = Path(__file__).parents[1]

# DESIGN-13.0-DOCKER-REWRITE.md section 6, verbatim.
RELEASE_COMMANDS = (
    ("deploy", "--target", "main"),
    ("deploy", "--target", "main", "--test"),
    ("test", "--target", "main"),
    ("build", "--target", "main"),
    ("select", "--target", "main", "--version", "13.0.0"),
    ("status", "--target", "main"),
    ("doctor",),
    ("install-unit", "--target", "main"),
)

# Names of things 13.0 section 8 deleted.  None of these may appear in a code
# block in an active document.
DELETED_COMMANDS = (
    "kei_release",
    "adopt-systemd",
    "configure-target",
    "record-live",
    "deploy-container.sh",
    "install-container-systemd",
    "materialize-release-evidence",
    "verify-release-evidence",
    "preflight-amd.sh",
    "preflight-compose.sh",
    "preflight-host.sh",
    "preflight-images.sh",
    "preflight-kvm.sh",
    "preflight-stack.sh",
    "bootstrap-toolbox-cache",
    "migrate-12",
    "migration12",
    "release.py candidate",
    "release.py promote",
    "release.py rollback",
)

# These never had a non-command meaning, so they are refused in prose too.
NEVER_ANYWHERE = ("kei_release", "adopt-systemd", "configure-target", "record-live")

ACTIVE_DOCS = ("README.md", "DEPLOYMENT.md", "config/cognita-compose.env.example")


def _code_blocks(text: str) -> list[str]:
    """Every fenced block plus every indented (4-space) block in a document."""
    blocks = re.findall(r"^```.*?^```", text, re.MULTILINE | re.DOTALL)
    indented: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("    ") and line.strip():
            current.append(line)
        elif current and not line.strip():
            current.append(line)
        elif current:
            indented.append("\n".join(current))
            current = []
    if current:
        indented.append("\n".join(current))
    return blocks + indented


def test_release_commands_are_supported_by_the_release_tool() -> None:
    spec = importlib.util.spec_from_file_location("cognita_release_docs", ROOT / "scripts" / "release.py")
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = release
    spec.loader.exec_module(release)
    parser = release.build_parser()
    for arguments in RELEASE_COMMANDS:
        parsed = parser.parse_args(arguments)
        assert parsed.command == arguments[0]
    reset_source = (ROOT / "scripts" / "reset_disposable_state.py").read_text(encoding="utf-8")
    assert 'add_argument("--scope", required=True' in reset_source


def test_active_docs_name_no_deleted_command() -> None:
    for name in ACTIVE_DOCS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for banned in NEVER_ANYWHERE:
            assert banned not in text, f"{name} still mentions {banned}"
        for block in _code_blocks(text):
            for banned in DELETED_COMMANDS:
                assert banned not in block, f"{name} has a code block running {banned}"


def test_changelog_has_the_current_section_and_the_13_0_removals() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # Every shipped version has a section...
    assert f"## {__version__}" in changelog
    # ...and the 13.0.0 section is the record of the rewrite and what went.
    section = changelog.split("## 13.0.0", 1)[1].split("\n## ", 1)[0]
    for phrase in ("release.py", "schema", "test mode", "reset_disposable_state.py", "### Removed"):
        assert phrase in section, phrase
    # The removal paragraph is the record of what went; it names the machinery.
    for phrase in ("kei_release.py", "preflight-", "migrate-12"):
        assert phrase in section, phrase


def test_release_notes_exist_for_the_current_version() -> None:
    notes = (ROOT / "docs" / f"RELEASE-{__version__}.md").read_text(encoding="utf-8")
    assert notes.startswith(f"# Cognita {__version__} release notes")
    # The 13.0.0 notes describe the rewrite itself; a later 13.0.x note only
    # has to exist and say what changed.
    rewrite = (ROOT / "docs" / "RELEASE-13.0.0.md").read_text(encoding="utf-8")
    for phrase in ("release tooling", "schema", "test mode", "reset_disposable_state.py"):
        assert phrase in rewrite, phrase


def test_deployment_covers_the_operator_contract() -> None:
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for phrase in (
        "127.0.0.1:8675/healthz",
        "port 8676",
        "./cognita update",
        "./cognita rollback",
        "cognita rollback",
        "cognita reset index|workspaces|all",
        "source documents are not managed",
    ):
        assert phrase in deployment, phrase


def test_readme_keeps_the_user_facts_and_spells_no_version() -> None:
    # The operator facts (env variables, the version authority) moved to DEPLOYMENT.md with the
    # 2026-09-29 rewrite; the README keeps what a user needs to know.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in ("Workspace", "Model Context Protocol", "Install on Linux", "Install on Windows"):
        assert phrase in readme, phrase
    identity = (ROOT / "src" / "cognita" / "release_identity.py").read_text(encoding="utf-8")
    assert "APPLICATION_VERSION =" in identity
    # The version is code-owned and never written down in the customer guide.
    assert not re.search(r"^# Cognita \d+\.\d+\.\d+", readme, re.MULTILINE)
    assert f"# Cognita {__version__}" not in readme


def test_env_template_is_pinned_and_publishes_no_private_service() -> None:
    env = (ROOT / "config" / "cognita-compose.env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    workspace_compose = (ROOT / "compose.workspace.yaml").read_text(encoding="utf-8")
    for variable in (
        "COGNITA_RELEASE_TARGET",
        "COGNITA_VERSION",
        "COGNITA_CONFIG_ROOT",
        "COGNITA_PROJECTS_ROOT",
        "COGNITA_POSTGRES_DATA_ROOT",
        "COGNITA_WORKSPACE_DATA_ROOT",
        "COGNITA_TRANSFER_STAGING_ROOT",
        "COGNITA_SECRETS_ROOT",
        "COGNITA_MODEL_CACHE_ROOT",
        "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT",
        "COGNITA_KVM_GID",
    ):
        assert f"{variable}=" in env, variable
    # The key must exist (Compose interpolates image names from it); its value
    # is whatever release.py exported for the run, so the template's example
    # value is not pinned to the current version.
    assert "COGNITA_VERSION=" in env
    assert "pgvector/pgvector:0.8.6-pg18-trixie@" in compose
    assert "microsandbox:0.7.0@" in workspace_compose
    assert "workspace-runtime:" not in compose
    assert "COGNITA_WORKSPACE_RUNTIME_URL" in workspace_compose
    assert "docker.sock" not in compose
    assert "privileged:" not in compose


def test_the_unit_template_starts_compose_and_nothing_else() -> None:
    template = (ROOT / "scripts" / "systemd" / "cognita-compose.service.template").read_text(
        encoding="utf-8"
    )
    assert "After=network-online.target" in template
    assert "docker compose" in template and "--wait" in template
    assert "up -d --no-build --pull never --wait" in template
    # 13.0 section 6.4: startup never builds, pulls, tests, migrates, restores
    # or resets, so the unit has no ExecStartPre gate at all.
    assert "ExecStartPre" not in template
    assert "/usr/bin/sg" not in template
    assert "getent group" not in template


def test_version_and_release_notes_are_consistent() -> None:
    """13.0 section 4: one authority, and a CHANGELOG entry that matches it.

    Moved from tests/test_release_docs_12.py, unchanged in substance.
    `pyproject.toml` and `src/cognita/__init__.py` no longer SPELL the version,
    so this follows the number to the two places a reader actually gets it
    from -- the authority module and the installed package metadata -- which
    also catches a build made before a bump.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = (ROOT / "src" / "cognita" / "__init__.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert __version__ == APPLICATION_VERSION
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "cognita.release_identity.APPLICATION_VERSION" }' in pyproject
    assert f'version = "{__version__}"' not in pyproject
    assert "from .release_identity import APPLICATION_VERSION as __version__" in package
    assert importlib.metadata.version("cognita") == __version__
    assert "## 12.2.0" in changelog
    assert "## 12.1.0" in changelog


def test_historical_release_notes_are_untouched() -> None:
    for name, phrase in (
        ("RELEASE-12.1.0.md", "source_unchanged"),
        ("RELEASE-12.2.0.md", "Canonical hash routes"),
        ("RELEASE-12.5.0.md", "Admin-owned AMD acceleration"),
        ("RELEASE-12.17.0.md", "Workspace receipts"),
    ):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert phrase in text, name
