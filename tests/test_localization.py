from __future__ import annotations

import json
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


@pytest.mark.parametrize("values", [{}, {"folder": "docs", "extra": "x"}])
def test_translate_requires_exact_placeholder_values(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="placeholder mismatch"):
        translate("en-US", "admin.project.folder_invalid", values)
