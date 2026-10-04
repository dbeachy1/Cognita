import pytest

from cognita.assets.metadata import (
    canonical_metadata,
    merge_metadata,
    prepare_metadata,
    search_projection,
)
from cognita.assets.models import AssetError


def test_metadata_server_fields_merge_and_projection():
    value = prepare_metadata(
        {"title": "Night", "tags": [" A ", "a"], "prompts": {"user": "draw"}},
        received_sha256="a" * 64,
    )
    assert value["kind"] == "image"
    assert value["tags"] == ["A"]
    assert "User prompt: draw" in search_projection(value, "night.png")
    merged = merge_metadata(
        {"tags": ["A"], "extensions": {"x": 1}}, {"tags": ["B"], "extensions": {"y": 2}}
    )
    assert merged["tags"] == ["B", "A"] and merged["extensions"] == {"x": 1, "y": 2}
    assert merge_metadata({"title": "old"}, {"title": None})["title"] == "old"


def test_unknown_metadata_fields_are_rejected():
    with pytest.raises(AssetError):
        canonical_metadata({"asset_id": "not caller owned"})
