"""Focused contracts for the persistent 10.1 synthetic fixture provisioner."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cognita.assets.png import scan_png
from cognita.connectors import ConnectorStore
from cognita.registry import Project, Registry
from cognita.selftest import ASSET_TEST_A, ASSET_TEST_B, build_self_test_plan
from cognita.selftest_fixtures import (
    FixtureProvisionError,
    load_manifest,
    provision_self_test,
)

PACKAGE_DATA = Path(__file__).parents[1] / "src" / "cognita" / "selftest_fixtures" / "data"


def _provision_args(tmp_path: Path) -> dict[str, Path]:
    return {
        "documents_dir": tmp_path / "documents",
        "data_dir": tmp_path / "data",
        "registry_path": tmp_path / "registry.yaml",
        "connectors_path": tmp_path / "connectors.yaml",
    }


def _production_policy(path: Path, *, mode: str = "all", access: str | None = "write") -> None:
    store = ConnectorStore(path)
    store.create(
        expected_revision=0,
        name="Cognita",
        project_mode=mode,
        default_access=access,
        project_access={"Self-Test": "write"} if mode == "selected" else {},
        project_names=["Self-Test"] if mode == "selected" else [],
    )


def test_manifest_allowlist_and_hashes_are_integrity_checked():
    specs = load_manifest()
    paths = {spec.path for spec in specs}
    assert paths == {
        "cognita-selftest/ocr/canonical-clear.png",
        "cognita-selftest/ocr/blank.png",
        "cognita-selftest/ocr/malformed.png",
        "cognita-selftest/ocr/animated.png",
        "cognita-selftest/ocr/not-a-png.txt",
        "cognita-selftest/ocr/over-limit.png",
    }
    # The retained lifecycle canaries are runtime-only asset evidence and must
    # never be copied, renamed, or treated as OCR package inputs.
    assert ASSET_TEST_A not in paths
    assert ASSET_TEST_B not in paths
    for spec in specs:
        source = PACKAGE_DATA / spec.path
        assert hashlib.sha256(source.read_bytes()).hexdigest() == spec.sha256
    assert next(s for s in specs if s.path.endswith("canonical-clear.png")).sha256 == (
        "f371c0951f5ad07b2c039b4481ad23e746f2db18bee014e5d54a6de734bb8a63"
    )


def test_failure_fixtures_are_deterministic_and_bounded():
    by_path = {spec.path: spec for spec in load_manifest()}
    malformed = PACKAGE_DATA / "cognita-selftest/ocr/malformed.png"
    animated = PACKAGE_DATA / "cognita-selftest/ocr/animated.png"
    over_limit = PACKAGE_DATA / "cognita-selftest/ocr/over-limit.png"
    with pytest.raises(Exception) as invalid:
        scan_png(malformed.read_bytes())
    with pytest.raises(Exception) as animated_error:
        scan_png(animated.read_bytes())
    with pytest.raises(Exception) as dimensions_error:
        scan_png(over_limit.read_bytes())
    assert invalid.value.reason == "invalid_png"
    assert animated_error.value.reason == "animated_png"
    assert dimensions_error.value.reason == "dimension_limit"
    assert hashlib.sha256((PACKAGE_DATA / "cognita-selftest/ocr/not-a-png.txt").read_bytes()).hexdigest() == by_path[
        "cognita-selftest/ocr/not-a-png.txt"
    ].sha256


def test_selftest_instructions_pin_the_provisioned_project_paths_and_hashes():
    plan = build_self_test_plan("10.1.0", readonly=True)
    for spec in load_manifest():
        assert f'cognita-selftest/ocr/{Path(spec.path).name}' in plan
        assert spec.sha256 in plan
    assert 'project="Self-Test"' in plan
    assert "blocked: fixture_provisioning" in plan
    assert "OCR CHECKS (10.1)" in plan
    assert "source-replacement races" in plan
    assert "cannot independently hash" in plan


def test_first_provision_idempotent_repair_and_preservation(tmp_path: Path):
    args = _provision_args(tmp_path)
    registry = Registry(args["registry_path"])
    registry.add(Project(name="Unrelated", documents_dir=tmp_path / "other-docs", data_dir=tmp_path / "other-data"))
    args["documents_dir"].mkdir(parents=True)
    (args["documents_dir"] / "keep.txt").write_text("unrelated", encoding="utf-8")
    _production_policy(args["connectors_path"])

    first = provision_self_test(**args)
    assert first.project_created is True
    assert len(first.copied) == 6
    assert (args["documents_dir"] / "keep.txt").read_text(encoding="utf-8") == "unrelated"
    assert Registry(args["registry_path"]).get("Unrelated") is not None
    assert Registry(args["registry_path"]).get("Self-Test").writable is True

    second = provision_self_test(**args)
    assert second.project_created is False
    assert second.copied == ()
    changed = args["documents_dir"] / "cognita-selftest/ocr/canonical-clear.png"
    changed.write_bytes(b"changed")
    repaired = provision_self_test(**args)
    assert repaired.copied == ("cognita-selftest/ocr/canonical-clear.png",)
    assert not list(args["documents_dir"].rglob("*.tmp"))


def test_conflicting_registry_refused_without_touching_target(tmp_path: Path):
    args = _provision_args(tmp_path)
    Registry(args["registry_path"]).add(
        Project(name="Self-Test", documents_dir=tmp_path / "wrong", data_dir=tmp_path / "data", writable=True)
    )
    _production_policy(args["connectors_path"])
    with pytest.raises(FixtureProvisionError, match="fixture_provisioning"):
        provision_self_test(**args)
    assert not args["documents_dir"].exists()


def test_conflicting_connector_policy_is_refused(tmp_path: Path):
    args = _provision_args(tmp_path)
    Registry(args["registry_path"]).add(
        Project(name="Self-Test", documents_dir=args["documents_dir"], data_dir=args["data_dir"])
    )
    _production_policy(args["connectors_path"], mode="selected", access=None)
    with pytest.raises(FixtureProvisionError, match="fixture_provisioning"):
        provision_self_test(**args)
    assert Registry(args["registry_path"]).get("Self-Test") is not None
