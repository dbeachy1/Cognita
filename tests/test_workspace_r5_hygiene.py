"""Focused R5 hygiene defaults for repository-controlled Workspace services."""

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_compose_services_use_bounded_rotated_local_logs() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert compose["x-cognita-logging"] == {
        "driver": "local",
        "options": {"max-size": "10m", "max-file": "3"},
    }
    for service in compose["services"].values():
        assert service["logging"] == compose["x-cognita-logging"]


def test_toolbox_package_download_caches_default_to_ephemeral_or_disabled() -> None:
    dockerfile = (ROOT / "containers/workspace-toolbox/Dockerfile").read_text(encoding="utf-8")

    assert "PIP_NO_CACHE_DIR=1" in dockerfile
    assert "npm_config_cache=/tmp/npm-cache" in dockerfile
    assert "rm -rf /var/lib/apt/lists/* /var/cache/apt/*" in dockerfile
