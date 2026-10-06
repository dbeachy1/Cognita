from __future__ import annotations

import hashlib
import struct

import pytest

from cognita.books.media import MediaValidationError, inspect_media, inspect_media_file
from cognita.books.models import RawFormat


def _chunk(tag: bytes, payload: bytes, *, endian: str = "<") -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return tag + struct.pack(endian + "I", len(payload)) + payload + padding


def _wav(samples: bytes, *, channels: int = 1, rate: int = 8000, bits: int = 16, code: int = 1) -> bytes:
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", code, channels, rate, rate * align, align, bits)
    body = b"WAVE" + _chunk(b"fmt ", fmt) + _chunk(b"data", samples)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _rf64(samples: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    ds64 = b"\x00" * 28
    body = b"WAVE" + _chunk(b"ds64", ds64) + _chunk(b"fmt ", fmt)
    body += b"data" + struct.pack("<I", 0xFFFFFFFF) + samples
    if len(samples) & 1:
        body += b"\x00"
    raw = b"RF64" + b"\xff\xff\xff\xff" + body
    ds64_start = 12 + 8
    raw = bytearray(raw)
    struct.pack_into("<QQQI", raw, ds64_start, len(raw) - 8, len(samples), len(samples) // 2, 0)
    return bytes(raw)


def test_headered_pcm_wav_facts_hash_exact_samples_and_bytes() -> None:
    samples = struct.pack("<hh", -1234, 2345)
    raw = _wav(samples, rate=44100)
    result = inspect_media(raw)
    assert result.bytes_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.size_bytes == len(raw)
    assert result.sample_bytes == samples and result.native_pcm is True
    assert result.media.codec == "pcm_s16le"
    assert result.media.container == "wav"
    assert result.media.frame_count == "2"
    assert result.media.duration_seconds == pytest.approx(2 / 44100)
    assert result.media.canonical_sample_sha256 == hashlib.sha256(samples).hexdigest()


def test_rifx_big_endian_samples_are_hashed_in_canonical_little_endian_order() -> None:
    samples = b"\x01\x02\xff\xfe"
    fmt = struct.pack(">HHIIHH", 1, 1, 22050, 44100, 2, 16)
    body = b"WAVE" + _chunk(b"fmt ", fmt, endian=">") + _chunk(b"data", samples, endian=">")
    raw = b"RIFX" + struct.pack(">I", len(body)) + body
    result = inspect_media(raw)
    assert result.sample_bytes == samples
    assert result.media.endianness == "big"
    assert result.media.canonical_sample_sha256 == hashlib.sha256(b"\x02\x01\xfe\xff").hexdigest()


def test_raw_pcm_requires_saved_matching_provider_evidence_and_validates_frames() -> None:
    fmt = RawFormat.model_validate({
        "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 16000,
        "channels": 2, "storage_bits": 16, "valid_bits": 16, "endianness": "big",
        "interleaving": "interleaved", "provider_format_evidence": "provider response format: s16be",
    }, strict=True)
    samples = b"\x01\x02\xff\xfe"
    with pytest.raises(MediaValidationError, match="saved provider format evidence"):
        inspect_media(samples, raw_format=fmt)
    with pytest.raises(MediaValidationError, match="differs from the saved provider evidence"):
        inspect_media(samples, raw_format=fmt, provider_format_evidence="different")
    result = inspect_media(samples, raw_format=fmt, provider_format_evidence=fmt.provider_format_evidence)
    assert result.media.container == "raw_pcm"
    assert result.media.frame_count == "1"
    assert result.media.canonical_sample_sha256 == hashlib.sha256(b"\x02\x01\xfe\xff").hexdigest()
    with pytest.raises(MediaValidationError, match="partial sample frame"):
        inspect_media(samples[:-1], raw_format=fmt, provider_format_evidence=fmt.provider_format_evidence)


def test_wav_rejects_partial_frames_and_nonfinite_float_samples() -> None:
    with pytest.raises(MediaValidationError, match="partial sample frame"):
        inspect_media(_wav(b"\x00", bits=16))
    nan_wav = _wav(struct.pack("<f", float("nan")), bits=32, code=3)
    with pytest.raises(MediaValidationError, match="nonfinite"):
        inspect_media(nan_wav)


def test_headered_pcm_metadata_must_match_ffprobe_and_rawformat() -> None:
    samples = struct.pack("<hh", 1, 2)
    raw = _wav(samples)
    probe = {
        "streams": [{
            "codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "8000",
            "channels": 1, "bits_per_sample": 16,
        }],
        "format": {"format_name": "wav"},
    }
    assert inspect_media(raw, ffprobe=probe).media.codec == "pcm_s16le"
    conflict = {**probe, "streams": [{**probe["streams"][0], "channels": 2}]}
    with pytest.raises(MediaValidationError, match="channel count conflicts"):
        inspect_media(raw, ffprobe=conflict)
    raw_format = RawFormat.model_validate({
        "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 8000,
        "channels": 2, "storage_bits": 16, "valid_bits": 16, "endianness": "little",
        "interleaving": "interleaved", "provider_format_evidence": "saved exact format",
    }, strict=True)
    with pytest.raises(MediaValidationError, match="does not match supplied source_format"):
        inspect_media(raw, raw_format=raw_format)


def test_compressed_audio_stays_compressed_and_requires_actual_probe_facts() -> None:
    raw = b"ID3" + b"synthetic compressed bytes"
    probe = {
        "streams": [{
            "codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
            "channels": 1, "duration": "1.25", "bit_rate": "96000", "nb_frames": "42",
        }],
        "format": {"format_name": "mp3", "duration": "1.25"},
    }
    with pytest.raises(MediaValidationError, match="requires actual ffprobe"):
        inspect_media(raw)
    result = inspect_media(raw, ffprobe=probe)
    assert result.native_pcm is False and result.sample_bytes is None
    assert result.media.encoding == "compressed"
    assert result.media.canonical_sample_sha256 is None
    assert result.media.frame_count == "42"
    with pytest.raises(MediaValidationError, match="exactly one"):
        inspect_media(raw, ffprobe={**probe, "streams": [*probe["streams"], *probe["streams"]]})


def test_rf64_pcm_sample_identity_matches_wav_and_ffprobe_evidence() -> None:
    samples = struct.pack("<hh", 42, -7)
    result = inspect_media(_rf64(samples))
    assert result.media.container == "rf64"
    assert result.media.frame_count == "2"
    assert result.media.canonical_sample_sha256 == hashlib.sha256(samples).hexdigest()


def test_silence_and_file_streaming_preserve_sample_ranges(tmp_path) -> None:
    raw = _wav(b"\x00\x00\x00\x00")
    path = tmp_path / "silence.wav"
    path.write_bytes(raw)
    inspected = inspect_media_file(path)
    assert inspected.size_bytes == len(raw)
    assert inspected.bytes_sha256 == hashlib.sha256(raw).hexdigest()
    assert inspected.media.frame_count == "2"
    assert inspected.media.canonical_sample_sha256 == hashlib.sha256(b"\x00\x00\x00\x00").hexdigest()
    assert inspected.sample_bytes is None
    assert len(inspected.sample_spans) == 1
    offset, length = inspected.sample_spans[0]
    assert raw[offset:offset + length] == b"\x00\x00\x00\x00"


def test_raw_pcm_file_requires_exact_evidence_and_streams_hash(tmp_path) -> None:
    source_format = RawFormat.model_validate({
        "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 8000,
        "channels": 1, "storage_bits": 16, "valid_bits": 16, "endianness": "big",
        "interleaving": "interleaved", "provider_format_evidence": "saved exact format",
    }, strict=True)
    data = struct.pack(">hh", 1, -2)
    path = tmp_path / "raw.pcm"
    path.write_bytes(data)
    inspected = inspect_media_file(
        path, raw_format=source_format, provider_format_evidence="saved exact format"
    )
    assert inspected.sample_spans == ((0, len(data)),)
    assert inspected.media.canonical_sample_sha256 == hashlib.sha256(struct.pack("<hh", 1, -2)).hexdigest()
