from __future__ import annotations

from types import SimpleNamespace

import django
import pytest
import yaml
from django.conf import settings
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error

from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore, build_connector_url, build_route_url
from cognita.registry import Registry

if not settings.configured:
    settings.configure(
        SECRET_KEY="oauth-policy-test",
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "oauth2_provider"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        OAUTH2_PROVIDER={},
    )
    django.setup()

from cognita.oauth_service.policy import ResourcePolicy  # noqa: E402

CONNECTOR_ID = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
CONNECTOR_SLUG = "cognita"


def _policy(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump({
            "version": 1,
            "projects": [{
                "name": "KEI",
                "documents_dir": str(tmp_path / "docs"),
                "data_dir": str(tmp_path / "project"),
                "enabled": True,
            }],
        }),
        encoding="utf-8",
    )
    connectors_path = tmp_path / "connectors.yaml"
    connectors_path.write_text(
        yaml.safe_dump({
            "version": 1,
            "revision": 1,
            "connectors": [{
                "id": CONNECTOR_ID,
                "name": "Cognita",
                "enabled": True,
                "project_mode": "all",
                "default_access": "write",
                "project_access": {},
            }],
        }),
        encoding="utf-8",
    )
    config = CognitaConfig(
        registry_path=registry_path,
        connectors_path=connectors_path,
        public_base_url="https://cognita.example",
    )
    store = ConnectorStore(connectors_path)
    return ResourcePolicy(config, Registry(registry_path), connector_store=store), store


def test_connector_resource_is_exact_and_resolves_current_access(tmp_path):
    policy, _store = _policy(tmp_path)
    resource = f"https://cognita.example/mcp/connectors/{CONNECTOR_SLUG}/mcp/v5"
    stable = f"https://cognita.example/mcp/connectors/{CONNECTOR_SLUG}/mcp"

    assert policy.resource_for(CONNECTOR_ID) == resource
    assert policy.connector_id_for(resource) == CONNECTOR_ID
    assert policy.connector_id_for(stable) == CONNECTOR_ID
    assert policy.validate(stable) == stable
    assert policy.connector_for(resource).name == "Cognita"
    assert [(item.project, item.access) for item in policy.accessible_projects(resource)] == [
        ("KEI", "write")
    ]
    assert policy.validate(resource) == resource


def test_retired_generation_is_not_an_oauth_resource(tmp_path):
    policy, _store = _policy(tmp_path)
    current = f"https://cognita.example/mcp/connectors/{CONNECTOR_SLUG}/mcp/v5"
    previous = f"https://cognita.example/mcp/connectors/{CONNECTOR_SLUG}/mcp/v4"

    assert policy.validate(current) == current
    assert policy.validate(current) == current
    assert policy.connector_id_for(previous) is None
    assert policy.connector_for(previous) is None
    assert policy.connection_summary(previous) is None
    with pytest.raises(CustomOAuth2Error) as raised:
        policy.validate(previous)
    assert raised.value.error == "invalid_target"


def test_stable_alias_is_a_distinct_valid_oauth_resource(tmp_path):
    policy, _store = _policy(tmp_path)
    current = policy.resource_for(CONNECTOR_ID)
    stable = build_route_url("https://cognita.example", "combined", CONNECTOR_SLUG)

    assert policy.validate(stable) == stable
    assert stable != current
    assert current.endswith("/mcp/v5")


def test_versioned_resources_honor_public_base_path_prefix(tmp_path):
    policy, _store = _policy(tmp_path)
    policy.config.public_base_url = "https://cognita.example/cognita"
    policy.config._deployment_public_base_url = policy.config.public_base_url
    current = build_route_url(policy.config.public_base_url, "combined", CONNECTOR_SLUG, 5)

    assert policy.validate(current) == current
    assert policy.connector_resource_for(
        "https://cognita.example/other/mcp/connectors/cognita/mcp"
    ) is None


@pytest.mark.parametrize(
    "resources",
    [
        [],
        [
            "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v2",
            "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v2",
        ],
        "https://cognita.example/mcp/KEI",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v0",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v01",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v+1",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v2/",
        "https://cognita.example/mcp/connectors/2C520A44-2037-4BB5-A565-D88EC2BB02D1",
        "/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v2",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1/mcp/v2?x=1",
        "https://cognita.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1?x=1",
        "https://other.example/mcp/connectors/2c520a44-2037-4bb5-a565-d88ec2bb02d1",
    ],
)
def test_legacy_or_noncanonical_resources_fail_with_stable_error(tmp_path, resources):
    policy, _store = _policy(tmp_path)

    with pytest.raises(CustomOAuth2Error) as raised:
        policy.validate(resources)

    assert raised.value.error == "invalid_target"


def test_disabled_connector_is_denied_after_policy_reload(tmp_path):
    policy, store = _policy(tmp_path)
    resource = policy.resource_for(CONNECTOR_ID)
    store.mutate(1, lambda config: setattr(config.connectors[0], "enabled", False))

    with pytest.raises(CustomOAuth2Error) as raised:
        policy.validate(resource)

    assert raised.value.error == "invalid_target"
    assert policy.connector_for(resource) is None


def test_request_resources_preserves_repeated_oauthlib_body_values():
    request = SimpleNamespace(
        resource="first",
        decoded_body=[("resource", "first"), ("resource", "second")],
    )
    assert ResourcePolicy.request_resources(request) == ["first", "second"]


def test_malformed_current_connector_policy_fails_closed(tmp_path):
    policy, store = _policy(tmp_path)
    resource = policy.resource_for(CONNECTOR_ID)
    store.path.write_text("version: 99\n", encoding="utf-8")

    with pytest.raises(CustomOAuth2Error) as raised:
        policy.validate(resource)

    assert raised.value.error == "invalid_target"


def test_connection_summary_tracks_current_connector_access(tmp_path):
    policy, store = _policy(tmp_path)
    resource = policy.resource_for(CONNECTOR_ID)

    assert policy.connection_summary(resource) == {
        "id": CONNECTOR_ID,
        "name": "Cognita",
        "enabled": True,
        "revision": 1,
        "projects": [{"name": "KEI", "access": "write"}],
    }

    store.update(
        CONNECTOR_ID,
        expected_revision=1,
        project_names=["KEI"],
        default_access="read",
    )
    changed = policy.connection_summary(resource)
    assert changed["revision"] == 2
    assert changed["projects"] == [{"name": "KEI", "access": "read"}]


def test_oauth_access_reloads_project_exclusion_and_explicit_override(tmp_path):
    policy, store = _policy(tmp_path)
    resource = policy.resource_for(CONNECTOR_ID)
    registry = Registry(policy.config.registry_path)

    registry.update_settings("KEI", exclude_from_default_permissions=True)
    assert policy.accessible_projects(resource) == []
    assert policy.connection_summary(resource)["projects"] == []

    store.update(
        CONNECTOR_ID,
        expected_revision=1,
        project_names=["KEI"],
        project_access={"KEI": "read"},
    )
    assert [(item.project, item.access) for item in policy.accessible_projects(resource)] == [
        ("KEI", "read")
    ]
    assert policy.connection_summary(resource)["projects"] == [
        {"name": "KEI", "access": "read"}
    ]


def test_only_current_generation_is_valid(tmp_path):
    policy, _store = _policy(tmp_path)

    current = policy.resource_for(CONNECTOR_ID)
    assert policy.validate(current) == current
    for retired_or_future in (1, 2, 3, 4, 6):
        resource = build_connector_url(
            "https://cognita.example", CONNECTOR_SLUG, retired_or_future,
        )
        assert policy.connector_resource_for(resource) is None
        assert policy.connector_id_for(resource) is None
        assert policy.connection_summary(resource) is None
        with pytest.raises(CustomOAuth2Error) as raised:
            policy.validate(resource)
        assert raised.value.error == "invalid_target"
