import base64

import pytest

from cognita.assets.limits import (
    MAX_DATA_URL_CHARS,
    MAX_INLINE_PNG_BYTES,
    MAX_PNG_BYTES,
    decode_base64,
    operation_id,
    validate_data_url,
)
from cognita.assets.models import AssetError
from cognita.config import CognitaConfig


def test_contract_limits_and_exact_data_url():
    assert MAX_PNG_BYTES == 16 * 1_048_576
    assert MAX_INLINE_PNG_BYTES == 1_048_576
    assert MAX_DATA_URL_CHARS == len("data:image/png;base64,") + 22_369_624
    assert CognitaConfig().asset_max_png_bytes == MAX_PNG_BYTES
    assert CognitaConfig().asset_max_chunk_bytes == MAX_PNG_BYTES
    assert validate_data_url("data:image/png;base64," + base64.b64encode(b"x").decode()) == "eA=="


@pytest.mark.parametrize("value", ["data:image/jpeg;base64,eA==", "data:image/png;base64, eA==", "http://example/a.png"])
def test_data_url_is_strict(value):
    with pytest.raises(AssetError):
        validate_data_url(value)


def test_operation_id_and_base64_validation():
    assert operation_id("retry:1") == "retry:1"
    with pytest.raises(AssetError):
        operation_id("bad id")
    with pytest.raises(AssetError):
        decode_base64("not-base64")
