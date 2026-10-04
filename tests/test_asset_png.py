import base64
import struct
import zlib

import pytest

from cognita.assets.limits import MAX_PNG_BYTES
from cognita.assets.models import AssetError
from cognita.assets.png import embed_metadata, scan_png

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_scan_and_embed_preserve_image_chunks():
    facts, chunks = scan_png(PNG)
    assert (facts.width, facts.height, facts.chunk_count) == (1, 1, 3)
    with_metadata = embed_metadata(
        PNG,
        {
            "schema": "urn:cognita:image-metadata:v1",
            "schema_version": 1,
            "asset_id": "a",
            "kind": "image",
        },
    )
    facts2, chunks2 = scan_png(with_metadata)
    assert facts2.cognita_chunks == 1
    assert b"".join(c.data for c in chunks if c.kind == b"IDAT") == b"".join(
        c.data for c in chunks2 if c.kind == b"IDAT"
    )


def test_apng_is_rejected():
    import struct
    import zlib

    payload = struct.pack(">II", 1, 0)
    chunk = (
        struct.pack(">I", len(payload))
        + b"acTL"
        + payload
        + struct.pack(">I", zlib.crc32(b"acTL" + payload) & 0xFFFFFFFF)
    )
    with pytest.raises(AssetError, match="animated"):
        scan_png(PNG[:33] + chunk + PNG[33:])


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def test_total_png_limit_is_enforced_before_chunk_walk():
    with pytest.raises(AssetError) as caught:
        scan_png(PNG + b"x" * MAX_PNG_BYTES)
    assert caught.value.reason == "byte_limit"


def test_dimension_and_pixel_limits_explain_size_refusals():
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 5_000, 1, 8, 6, 0, 0, 0))
    image = PNG[:8] + ihdr + PNG[33:]
    with pytest.raises(AssetError) as caught:
        scan_png(image, dimension_limit=4_096)
    assert caught.value.reason == "dimension_limit"
    assert "4,096-pixel" in caught.value.message

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 4_000, 4_000, 8, 6, 0, 0, 0))
    image = PNG[:8] + ihdr + PNG[33:]
    with pytest.raises(AssetError) as caught:
        scan_png(image, dimension_limit=4_096, pixel_limit=1_000_000)
    assert caught.value.reason == "dimension_limit"
    assert "1,000,000-pixel" in caught.value.message


def test_compressed_cognita_metadata_has_a_hard_inflate_limit():
    bomb = zlib.compress(b"x" * 10_000_000)
    payload = b"Cognita\0\0\0\0" + bomb
    image = PNG[:-12] + _chunk(b"iTXt", payload) + PNG[-12:]
    with pytest.raises(AssetError) as caught:
        scan_png(image)
    assert caught.value.reason == "metadata_limit"
