from pathlib import Path

import pytest

from cognita.registry import NAME_RE, Project, Registry
from cognita.tokens import generate_token, hash_token


def make_registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry.yaml")


def test_empty_registry_when_file_missing(tmp_path):
    reg = make_registry(tmp_path)
    assert reg.projects == []


def test_add_save_reload_roundtrip(tmp_path):
    reg = make_registry(tmp_path)
    reg.add(
        Project(
            name="KEI",
            documents_dir=tmp_path / "docs",
            data_dir=tmp_path / "data" / "KEI",
            token_sha256=hash_token("secret"),
        )
    )
    reg2 = make_registry(tmp_path)
    assert len(reg2.projects) == 1
    p = reg2.get("KEI")
    assert p is not None
    assert p.enabled
    assert p.created_at  # stamped on add


def test_duplicate_name_rejected(tmp_path):
    reg = make_registry(tmp_path)
    reg.add(Project(name="a", documents_dir=tmp_path, data_dir=tmp_path))
    with pytest.raises(ValueError):
        reg.add(Project(name="a", documents_dir=tmp_path, data_dir=tmp_path))


def test_invalid_names_rejected(tmp_path):
    for bad in ["-lead", "has space", "sla/sh", "", "dot.dot"]:
        with pytest.raises(Exception):
            Project(name=bad, documents_dir=tmp_path, data_dir=tmp_path)


def test_find_by_token_routes_to_correct_project(tmp_path):
    reg = make_registry(tmp_path)
    tok_a, tok_b = generate_token(), generate_token()
    reg.add(Project(name="a", documents_dir=tmp_path, data_dir=tmp_path,
                    token_sha256=hash_token(tok_a)))
    reg.add(Project(name="b", documents_dir=tmp_path, data_dir=tmp_path,
                    token_sha256=hash_token(tok_b)))
    assert reg.find_by_token(tok_a).name == "a"
    assert reg.find_by_token(tok_b).name == "b"
    assert reg.find_by_token("wrong") is None


def test_disabled_project_token_rejected(tmp_path):
    reg = make_registry(tmp_path)
    tok = generate_token()
    reg.add(Project(name="a", documents_dir=tmp_path, data_dir=tmp_path,
                    token_sha256=hash_token(tok), enabled=False))
    assert reg.find_by_token(tok) is None


def test_remove(tmp_path):
    reg = make_registry(tmp_path)
    reg.add(Project(name="a", documents_dir=tmp_path, data_dir=tmp_path))
    reg.remove("a")
    assert make_registry(tmp_path).projects == []
    with pytest.raises(KeyError):
        reg.remove("a")


def test_project_name_rejects_a_trailing_newline():
    """Python's $ also matches just before a terminal newline, so "KEI\n"
    validated as a project name — and store.schema_for then built proj_KEI\n,
    a legal, correctly quoted, DIFFERENT identifier from proj_KEI. Two registry
    entries that read as the same project would own two separate schemas."""
    assert NAME_RE.match("KEI") is not None
    assert NAME_RE.match("Cognita-Kei") is not None
    assert NAME_RE.match("KEI\n") is None
    assert NAME_RE.match("\nKEI") is None


def test_exclude_from_default_permissions_defaults_and_round_trips(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        "version: 1\nprojects:\n  - name: OLD\n    documents_dir: .\n    data_dir: ./data\n",
        encoding="utf-8",
    )
    registry = Registry(path)
    assert registry.get("OLD").exclude_from_default_permissions is False
    registry.add(
        Project(
            name="NEW", documents_dir=tmp_path, data_dir=tmp_path / "data",
            exclude_from_default_permissions=True,
        )
    )
    reloaded = Registry(path)
    assert reloaded.get("NEW").exclude_from_default_permissions is True
    assert reloaded.get("OLD").exclude_from_default_permissions is False


@pytest.mark.parametrize("value", ["true", 1, 0, "false"])
def test_exclude_from_default_permissions_rejects_non_boolean(value, tmp_path):
    with pytest.raises(Exception):
        Project(
            name="KEI", documents_dir=tmp_path, data_dir=tmp_path,
            exclude_from_default_permissions=value,
        )


def test_registry_with_malformed_exclusion_fails_closed(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        "version: 1\nprojects:\n  - name: KEI\n    documents_dir: .\n    data_dir: ./data\n    exclude_from_default_permissions: definitely\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        Registry(path)


def test_exclude_settings_save_failure_rolls_back_in_memory(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(registry, "save", fail_save)
    with pytest.raises(OSError):
        registry.update_settings("KEI", exclude_from_default_permissions=True)
    assert registry.get("KEI").exclude_from_default_permissions is False
