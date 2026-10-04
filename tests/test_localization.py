from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cognita.localization import (
    SUPPORTED_LOCALES,
    load_catalog,
    resolve_locale,
    translate,
    validate_catalogs,
)


def test_resolves_supported_and_regionless_locales() -> None:
    assert resolve_locale("es") == "es-ES"
    assert resolve_locale("pt-BR") == "pt-BR"
    assert resolve_locale("pt-PT") == "en-US"
    assert resolve_locale("fr_CA") == "en-US"
    assert len(SUPPORTED_LOCALES) == 6


def test_all_admin_catalogs_have_matching_keys_and_placeholders() -> None:
    validate_catalogs()
    assert translate("fr-FR", "admin.project.folder_invalid", {"folder": "mes docs"}) == (
        "Le dossier mes docs n’est pas valide."
    )


def test_workspace_network_editor_and_configured_key_strings_are_localized() -> None:
    for locale in SUPPORTED_LOCALES:
        catalog = load_catalog(locale)
        assert catalog["admin.authentication.key_configured"]
        assert catalog["admin.status.unknown"]
        assert translate(locale, "admin.authentication.key_configured", {
            "key_id": "key-123", "created": "Oct 4",
        })
        assert "key-123" in translate(locale, "admin.authentication.key_configured", {
            "key_id": "key-123", "created": "Oct 4",
        })
        assert translate(locale, "admin.workspace.network.ports_integers", {"domain": "docs.example"})
        assert "docs.example" in translate(locale, "admin.workspace.network.ports_range", {"domain": "docs.example"})


def test_tagged_admin_markup_ids_exist_in_every_catalog() -> None:
    html_root = Path(__file__).resolve().parents[1] / "src/cognita/web"
    ids = set()
    for filename in ("index.html", "login.html"):
        markup = (html_root / filename).read_text(encoding="utf-8")
        ids.update(re.findall(r'data-i18n="([^"]+)"', markup))
        for value in re.findall(r'data-i18n-attr="([^"]+)"', markup):
            ids.add(value.split(":", 1)[1])
    for locale in SUPPORTED_LOCALES:
        catalog = load_catalog(locale)
        assert ids <= set(catalog), f"{locale} is missing {sorted(ids - set(catalog))}"


def test_browser_literal_message_ids_exist_in_every_catalog() -> None:
    web_root = Path(__file__).resolve().parents[1] / "src/cognita/web"
    ids = set()
    for filename in ("app.js", "admin-locale.js", "index.html", "login.html"):
        source = (web_root / filename).read_text(encoding="utf-8")
        ids.update(re.findall(r'''["']((?:admin|oauth)\.[\w.]+)["']''', source))
    for locale in SUPPORTED_LOCALES:
        catalog = load_catalog(locale)
        assert ids <= set(catalog), f"{locale} is missing {sorted(ids - set(catalog))}"


def test_translate_falls_back_only_for_missing_ids(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src/cognita/web/locales"
    for locale in SUPPORTED_LOCALES:
        (tmp_path / f"{locale}.json").write_text(
            (source / f"{locale}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
    data = json.loads((tmp_path / "fr-FR.json").read_text(encoding="utf-8"))
    del data["admin.language.label"]
    (tmp_path / "fr-FR.json").write_text(json.dumps(data), encoding="utf-8")
    # Explicit test directories are resolved independently from the packaged catalogs.
    assert translate("fr-FR", "admin.language.label", catalog_dir=tmp_path) == "Language"


def test_missing_catalog_uses_english_at_runtime_but_validation_fails(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src/cognita/web/locales/en-US.json"
    (tmp_path / "en-US.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    assert translate("pt-BR", "admin.language.label", catalog_dir=tmp_path) == "Language"
    with pytest.raises(FileNotFoundError):
        validate_catalogs(catalog_dir=tmp_path)


@pytest.mark.parametrize("values", [{}, {"folder": "docs", "extra": "x"}])
def test_translate_requires_exact_placeholder_values(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="placeholder mismatch"):
        translate("en-US", "admin.project.folder_invalid", values)
