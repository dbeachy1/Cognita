"""Pure audio metadata and PCM validation for audiobook imports.

Provider bytes remain the durable source. This module only validates their
declared/detected facts and computes the canonical sample identity used by
later storage and assembly code.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .models import MediaProperties, RawFormat


class MediaValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MediaInspection:
    media: MediaProperties
    bytes_sha256: str
    size_bytes: int
    sample_bytes: bytes | None
    native_pcm: bool
    sample_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class _PcmFormat:
    encoding: str
    sample_rate_hz: int
    channels: int
    storage_bits: int
    valid_bits: int
    endianness: str


def _safe_positive(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise MediaValidationError("invalid_media_metadata", f"{label} must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdecimal():
        number = int(value)
    else:
        raise MediaValidationError("invalid_media_metadata", f"{label} must be a positive integer")
    if number <= 0 or number > (1 << 53) - 1:
        raise MediaValidationError("invalid_media_metadata", f"{label} is outside the supported integer range")
    return number


def _format_values(value: RawFormat | Mapping[str, object]) -> dict[str, object]:
    if isinstance(value, RawFormat):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise MediaValidationError("invalid_media_metadata", "source_format must be a validated RawFormat object")


def _raw_pcm_format(value: RawFormat | Mapping[str, object]) -> _PcmFormat:
    try:
        model = RawFormat.model_validate(_format_values(value), strict=True)
    except Exception as exc:
        raise MediaValidationError("invalid_media_metadata", "Raw PCM format does not match the strict contract") from exc
    return _PcmFormat(
        model.encoding,
        model.sample_rate_hz,
        model.channels,
        model.storage_bits,
        model.valid_bits,
        model.endianness,
    )


def _validate_pcm_format(fmt: _PcmFormat) -> None:
    if fmt.storage_bits != fmt.valid_bits:
        raise MediaValidationError("unsupported_media", "Packed valid-bit PCM is not supported")
    allowed = {16, 24, 32} if fmt.encoding == "signed_integer" else {32, 64}
    if fmt.storage_bits not in allowed:
        raise MediaValidationError("unsupported_media", "PCM storage width is not supported")
    if fmt.endianness not in {"little", "big"}:
        raise MediaValidationError("invalid_media_metadata", "PCM endianness must be little or big")
    if fmt.channels * (fmt.storage_bits // 8) > 1024 * 1024:
        raise MediaValidationError("unsupported_media", "PCM sample frame exceeds the streaming block limit")


def _canonical_samples(samples: bytes, fmt: _PcmFormat) -> bytes:
    _validate_pcm_format(fmt)
    bytes_per_sample = fmt.storage_bits // 8
    frame_bytes = bytes_per_sample * fmt.channels
    if frame_bytes <= 0 or len(samples) % frame_bytes:
        raise MediaValidationError("invalid_audio", "PCM payload ends with a partial sample frame")
    if fmt.encoding == "float":
        code = "f" if fmt.storage_bits == 32 else "d"
        endian = "<" if fmt.endianness == "little" else ">"
        for (sample,) in struct.iter_unpack(endian + code, samples):
            if not math.isfinite(sample):
                raise MediaValidationError("invalid_audio", "PCM payload contains a nonfinite float sample")
    if fmt.endianness == "little":
        return samples
    # Convert each sample independently while retaining the declared width and
    # bit pattern. Channel order and frame order remain unchanged.
    return b"".join(
        samples[index:index + bytes_per_sample][::-1]
        for index in range(0, len(samples), bytes_per_sample)
    )


def _codec_name(fmt: _PcmFormat) -> str:
    kind = "s" if fmt.encoding == "signed_integer" else "f"
    suffix = "le" if fmt.endianness == "little" else "be"
    return f"pcm_{kind}{fmt.storage_bits}{suffix}"


def _media_properties(
    *, codec: str, container: str, fmt: _PcmFormat, sample_bytes: bytes,
    bitrate_bps: int | None = None,
) -> MediaProperties:
    canonical = _canonical_samples(sample_bytes, fmt)
    frame_bytes = fmt.channels * fmt.storage_bits // 8
    frames = len(sample_bytes) // frame_bytes
    return MediaProperties.model_validate({
        "codec": codec,
        "container": container,
        "sample_rate_hz": fmt.sample_rate_hz,
        "channels": fmt.channels,
        "encoding": fmt.encoding,
        "storage_bits": fmt.storage_bits,
        "valid_bits": fmt.valid_bits,
        "endianness": fmt.endianness,
        "bitrate_bps": bitrate_bps,
        "frame_count": str(frames),
        "duration_seconds": frames / fmt.sample_rate_hz,
        "canonical_sample_sha256": hashlib.sha256(canonical).hexdigest(),
    }, strict=True)


def _wave_pcm(raw: bytes) -> tuple[_PcmFormat, bytes, str]:
    if len(raw) < 12 or raw[8:12] != b"WAVE" or raw[:4] not in {b"RIFF", b"RIFX", b"RF64"}:
        raise MediaValidationError("invalid_audio", "Audio container is not RIFF/RIFX/RF64 WAVE")
    container = "rf64" if raw[:4] == b"RF64" else "wav"
    endian = ">" if raw[:4] == b"RIFX" else "<"
    limit = len(raw)
    if raw[:4] != b"RF64":
        declared = struct.unpack_from(endian + "I", raw, 4)[0] + 8
        if declared < 12 or declared > len(raw):
            raise MediaValidationError("invalid_audio", "WAVE declared size exceeds the available file bytes")
        limit = declared
    offset = 12
    ds64_data_size: int | None = None
    ds64_riff_size: int | None = None
    fmt_chunk: bytes | None = None
    data_chunks: list[bytes] = []
    while offset + 8 <= limit:
        chunk_id = raw[offset:offset + 4]
        size32 = struct.unpack_from(endian + "I", raw, offset + 4)[0]
        payload_start = offset + 8
        if chunk_id == b"ds64":
            if size32 < 28 or payload_start + size32 > limit:
                raise MediaValidationError("invalid_audio", "RF64 ds64 chunk is malformed")
            ds64_riff_size = struct.unpack_from("<Q", raw, payload_start)[0]
            ds64_data_size = struct.unpack_from("<Q", raw, payload_start + 8)[0]
            if ds64_riff_size + 8 > len(raw):
                raise MediaValidationError("invalid_audio", "RF64 declared size exceeds the available file bytes")
            limit = ds64_riff_size + 8
        chunk_size = ds64_data_size if raw[:4] == b"RF64" and chunk_id == b"data" and size32 == 0xFFFFFFFF else size32
        if chunk_size is None:
            raise MediaValidationError("invalid_audio", "RF64 data chunk is missing its ds64 size")
        payload_end = payload_start + chunk_size
        if payload_end > limit:
            raise MediaValidationError("invalid_audio", "WAVE chunk exceeds the available file bytes")
        padded_end = payload_end + (chunk_size & 1)
        if padded_end > limit:
            raise MediaValidationError("invalid_audio", "WAVE odd-sized chunk is missing its padding byte")
        if chunk_id == b"fmt ":
            if fmt_chunk is not None:
                raise MediaValidationError("invalid_audio", "WAVE has multiple format chunks")
            fmt_chunk = raw[payload_start:payload_end]
        elif chunk_id == b"data":
            data_chunks.append(raw[payload_start:payload_end])
        offset = padded_end
    if fmt_chunk is None or not data_chunks or not any(len(chunk) for chunk in data_chunks):
        raise MediaValidationError("invalid_audio", "WAVE must contain format and audio data chunks")
    if offset != limit:
        raise MediaValidationError("invalid_audio", "WAVE ends with an incomplete chunk header")
    if len(fmt_chunk) < 16:
        raise MediaValidationError("invalid_audio", "WAVE format chunk is too short")
    code, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(endian + "HHIIHH", fmt_chunk, 0)
    valid_bits = bits
    if code == 0xFFFE:
        if len(fmt_chunk) < 40 or struct.unpack_from(endian + "H", fmt_chunk, 16)[0] < 22:
            raise MediaValidationError("invalid_audio", "WAVE extensible format chunk is incomplete")
        valid_bits = struct.unpack_from(endian + "H", fmt_chunk, 18)[0]
        subformat = fmt_chunk[24:40]
        pcm_guid = bytes.fromhex("0100000000001000800000aa00389b71")
        float_guid = bytes.fromhex("0300000000001000800000aa00389b71")
        if subformat == pcm_guid:
            code = 1
        elif subformat == float_guid:
            code = 3
        else:
            raise MediaValidationError("unsupported_media", "Compressed WAVE extensible subformats are unsupported")
    if code not in {1, 3}:
        raise MediaValidationError("unsupported_media", "WAVE codec is not integer or IEEE float PCM")
    fmt = _PcmFormat(
        "signed_integer" if code == 1 else "float",
        _safe_positive(sample_rate, "sample rate"),
        _safe_positive(channels, "channel count"),
        _safe_positive(bits, "storage bits"),
        _safe_positive(valid_bits, "valid bits"),
        "big" if endian == ">" else "little",
    )
    _validate_pcm_format(fmt)
    expected_align = fmt.channels * fmt.storage_bits // 8
    if block_align != expected_align or byte_rate != fmt.sample_rate_hz * expected_align:
        raise MediaValidationError("invalid_audio", "WAVE format block alignment or byte rate is inconsistent")
    samples = b"".join(data_chunks)
    if len(samples) % block_align:
        raise MediaValidationError("invalid_audio", "WAVE data ends with a partial sample frame")
    return fmt, samples, container


def _assert_matches_raw_format(actual: _PcmFormat, supplied: RawFormat | Mapping[str, object]) -> None:
    expected = _raw_pcm_format(supplied)
    if actual != expected:
        raise MediaValidationError("media_mismatch", "Detected headered PCM does not match supplied source_format")


def _parse_probe(probe: Mapping[str, object]) -> MediaProperties:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise MediaValidationError("invalid_media_metadata", "ffprobe did not return a stream list")
    audio = [stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise MediaValidationError("unsupported_media", "Media must contain exactly one detected audio stream")
    stream = audio[0]
    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec:
        raise MediaValidationError("invalid_media_metadata", "ffprobe audio stream has no codec name")
    if codec.startswith("pcm_"):
        raise MediaValidationError("unsupported_media", "Uncompressed non-WAVE codecs need a verified sample reader")
    rate = _safe_positive(stream.get("sample_rate"), "sample rate")
    channels = _safe_positive(stream.get("channels"), "channel count")
    format_info = probe.get("format")
    fmt = format_info if isinstance(format_info, Mapping) else {}
    container_raw = fmt.get("format_name")
    container = container_raw.split(",", 1)[0] if isinstance(container_raw, str) and container_raw else "unknown"
    bitrate_value = stream.get("bit_rate", fmt.get("bit_rate"))
    bitrate = _safe_positive(bitrate_value, "bitrate") if bitrate_value not in (None, "N/A", "") else None
    duration_value = stream.get("duration", fmt.get("duration"))
    if duration_value in (None, "N/A", ""):
        raise MediaValidationError("invalid_media_metadata", "ffprobe audio duration is unavailable")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise MediaValidationError("invalid_media_metadata", "ffprobe duration is malformed") from exc
    if not math.isfinite(duration) or duration < 0:
        raise MediaValidationError("invalid_media_metadata", "ffprobe duration must be finite and nonnegative")
    frame_count_value = stream.get("nb_frames")
    frame_count = None
    if frame_count_value not in (None, "N/A", ""):
        frame_count = str(_safe_positive(frame_count_value, "frame count"))
    bits_value = stream.get("bits_per_raw_sample", stream.get("bits_per_sample"))
    # FFprobe reports bits_per_sample=0 for compressed MP3 streams. It means
    # the codec does not expose a PCM storage width, not an invalid zero-bit
    # audio format; compressed sample width is therefore unknown here.
    bits = _safe_positive(bits_value, "sample bits") if bits_value not in (None, "N/A", "", 0, "0") else None
    return MediaProperties.model_validate({
        "codec": codec,
        "container": container,
        "sample_rate_hz": rate,
        "channels": channels,
        "encoding": "compressed",
        "storage_bits": bits,
        "valid_bits": bits,
        "endianness": "not_applicable",
        "bitrate_bps": bitrate,
        "frame_count": frame_count,
        "duration_seconds": duration,
        "canonical_sample_sha256": None,
    }, strict=True)


def _probe_audio_stream(probe: Mapping[str, object]) -> Mapping[str, object]:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise MediaValidationError("invalid_media_metadata", "ffprobe did not return a stream list")
    audio = [stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise MediaValidationError("unsupported_media", "Media must contain exactly one detected audio stream")
    return audio[0]


def _assert_pcm_matches_probe(fmt: _PcmFormat, probe: Mapping[str, object]) -> None:
    stream = _probe_audio_stream(probe)
    codec = stream.get("codec_name")
    if not isinstance(codec, str) or codec != _codec_name(fmt):
        raise MediaValidationError("media_mismatch", "WAVE PCM codec conflicts with ffprobe stream metadata")
    if _safe_positive(stream.get("sample_rate"), "sample rate") != fmt.sample_rate_hz:
        raise MediaValidationError("media_mismatch", "WAVE sample rate conflicts with ffprobe")
    if _safe_positive(stream.get("channels"), "channel count") != fmt.channels:
        raise MediaValidationError("media_mismatch", "WAVE channel count conflicts with ffprobe")
    bits_value = stream.get("bits_per_raw_sample", stream.get("bits_per_sample"))
    if bits_value not in (None, "N/A", "", 0, "0") and _safe_positive(bits_value, "sample bits") != fmt.storage_bits:
        raise MediaValidationError("media_mismatch", "WAVE sample width conflicts with ffprobe")


def inspect_media(
    raw: bytes,
    *,
    ffprobe: Mapping[str, object] | None = None,
    raw_format: RawFormat | Mapping[str, object] | None = None,
    provider_format_evidence: str | None = None,
) -> MediaInspection:
    """Inspect one complete media object without rewriting or decoding it."""
    if not isinstance(raw, bytes) or not raw:
        raise MediaValidationError("invalid_audio", "Media bytes must be a nonempty byte string")
    raw_hash = hashlib.sha256(raw).hexdigest()
    is_wave = len(raw) >= 12 and raw[8:12] == b"WAVE" and raw[:4] in {b"RIFF", b"RIFX", b"RF64"}
    if raw_format is not None and not is_wave:
        if ffprobe is not None and _parse_probe(ffprobe).encoding == "compressed":
            raise MediaValidationError("media_mismatch", "Raw PCM metadata cannot override detected compressed media")
        if not isinstance(provider_format_evidence, str) or not provider_format_evidence.strip():
            raise MediaValidationError("invalid_media_metadata", "Headerless PCM requires saved provider format evidence")
        fmt = _raw_pcm_format(raw_format)
        values = _format_values(raw_format)
        if values.get("provider_format_evidence") != provider_format_evidence:
            raise MediaValidationError("media_mismatch", "RawFormat evidence differs from the saved provider evidence")
        samples = raw
        frame_bytes = fmt.channels * fmt.storage_bits // 8
        if len(samples) % frame_bytes:
            raise MediaValidationError("invalid_audio", "Raw PCM payload ends with a partial sample frame")
        media = _media_properties(codec=_codec_name(fmt), container="raw_pcm", fmt=fmt, sample_bytes=samples)
        if ffprobe is not None:
            _assert_pcm_matches_probe(fmt, ffprobe)
        return MediaInspection(media, raw_hash, len(raw), samples, True)

    if is_wave:
        fmt, samples, container = _wave_pcm(raw)
        media = _media_properties(codec=_codec_name(fmt), container=container, fmt=fmt, sample_bytes=samples)
        if ffprobe is not None:
            _assert_pcm_matches_probe(fmt, ffprobe)
        if raw_format is not None:
            _assert_matches_raw_format(fmt, raw_format)
        return MediaInspection(media, raw_hash, len(raw), samples, True)

    if ffprobe is None:
        raise MediaValidationError("unsupported_media", "Headered non-WAVE audio requires actual ffprobe metadata")
    media = _parse_probe(ffprobe)
    return MediaInspection(media, raw_hash, len(raw), None, False)


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _ensure_file_unchanged(path: Path, before: os.stat_result) -> None:
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise MediaValidationError("media_changed", "Media file changed during inspection")


def _wave_file_layout(path: Path) -> tuple[_PcmFormat, str, tuple[tuple[int, int], ...]]:
    file_size = path.stat().st_size
    with path.open("rb") as source:
        head = source.read(12)
        if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in {b"RIFF", b"RIFX", b"RF64"}:
            raise MediaValidationError("invalid_audio", "Audio container is not RIFF/RIFX/RF64 WAVE")
        is_rf64 = head[:4] == b"RF64"
        endian = ">" if head[:4] == b"RIFX" else "<"
        limit = file_size
        if not is_rf64:
            declared_end = struct.unpack_from(endian + "I", head, 4)[0] + 8
            if declared_end < 12 or declared_end > file_size:
                raise MediaValidationError("invalid_audio", "WAVE declared size exceeds the available file bytes")
            limit = declared_end
        ds64_data_size: int | None = None
        fmt_chunk: bytes | None = None
        spans: list[tuple[int, int]] = []
        cursor = 12
        while cursor + 8 <= limit:
            source.seek(cursor)
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                raise MediaValidationError("invalid_audio", "WAVE chunk header is truncated")
            chunk_id = chunk_header[:4]
            size32 = struct.unpack_from(endian + "I", chunk_header, 4)[0]
            payload_start = cursor + 8
            if chunk_id == b"ds64":
                if size32 < 28 or payload_start + size32 > limit:
                    raise MediaValidationError("invalid_audio", "RF64 ds64 chunk is malformed")
                source.seek(payload_start)
                ds64 = source.read(28)
                riff_size, ds64_data_size = struct.unpack_from("<QQ", ds64, 0)
                if riff_size + 8 > file_size:
                    raise MediaValidationError("invalid_audio", "RF64 declared size exceeds the available file bytes")
                limit = riff_size + 8
            chunk_size = ds64_data_size if is_rf64 and chunk_id == b"data" and size32 == 0xFFFFFFFF else size32
            if chunk_size is None:
                raise MediaValidationError("invalid_audio", "RF64 data chunk is missing its ds64 size")
            payload_end = payload_start + chunk_size
            if payload_end > limit:
                raise MediaValidationError("invalid_audio", "WAVE chunk exceeds the available file bytes")
            padded_end = payload_end + (chunk_size & 1)
            if padded_end > limit:
                raise MediaValidationError("invalid_audio", "WAVE odd-sized chunk is missing its padding byte")
            if chunk_id == b"fmt ":
                if fmt_chunk is not None or chunk_size > 65_536:
                    raise MediaValidationError("invalid_audio", "WAVE format chunk is duplicated or too large")
                source.seek(payload_start)
                fmt_chunk = source.read(chunk_size)
            elif chunk_id == b"data":
                spans.append((payload_start, chunk_size))
            cursor = padded_end
        if fmt_chunk is None or not spans or not any(length for _, length in spans):
            raise MediaValidationError("invalid_audio", "WAVE must contain format and audio data chunks")
        if cursor != limit:
            raise MediaValidationError("invalid_audio", "WAVE ends with an incomplete chunk header")
        if len(fmt_chunk) < 16:
            raise MediaValidationError("invalid_audio", "WAVE format chunk is too short")
        code, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(endian + "HHIIHH", fmt_chunk, 0)
        valid_bits = bits
        if code == 0xFFFE:
            if len(fmt_chunk) < 40 or struct.unpack_from(endian + "H", fmt_chunk, 16)[0] < 22:
                raise MediaValidationError("invalid_audio", "WAVE extensible format chunk is incomplete")
            valid_bits = struct.unpack_from(endian + "H", fmt_chunk, 18)[0]
            subformat = fmt_chunk[24:40]
            if subformat == bytes.fromhex("0100000000001000800000aa00389b71"):
                code = 1
            elif subformat == bytes.fromhex("0300000000001000800000aa00389b71"):
                code = 3
            else:
                raise MediaValidationError("unsupported_media", "Compressed WAVE extensible subformats are unsupported")
        if code not in {1, 3}:
            raise MediaValidationError("unsupported_media", "WAVE codec is not integer or IEEE float PCM")
        fmt = _PcmFormat(
            "signed_integer" if code == 1 else "float",
            _safe_positive(sample_rate, "sample rate"),
            _safe_positive(channels, "channel count"),
            _safe_positive(bits, "storage bits"),
            _safe_positive(valid_bits, "valid bits"),
            "big" if endian == ">" else "little",
        )
        _validate_pcm_format(fmt)
        expected_align = fmt.channels * fmt.storage_bits // 8
        if block_align != expected_align or byte_rate != fmt.sample_rate_hz * expected_align:
            raise MediaValidationError("invalid_audio", "WAVE format block alignment or byte rate is inconsistent")
        if any(length % expected_align for _, length in spans):
            raise MediaValidationError("invalid_audio", "WAVE data ends with a partial sample frame")
        return fmt, "rf64" if is_rf64 else "wav", tuple(spans)


def _hash_sample_spans(path: Path, spans: tuple[tuple[int, int], ...], fmt: _PcmFormat) -> tuple[str, int]:
    digest = hashlib.sha256()
    bytes_per_sample = fmt.storage_bits // 8
    frame_bytes = bytes_per_sample * fmt.channels
    total_frames = 0
    with path.open("rb") as source:
        for start, length in spans:
            source.seek(start)
            remaining = length
            total_frames += length // frame_bytes
            while remaining:
                count = min(1024 * 1024, remaining)
                count -= count % frame_bytes
                block = source.read(count)
                if len(block) != count:
                    raise MediaValidationError("invalid_audio", "WAVE sample data changed or was truncated during inspection")
                remaining -= count
                if fmt.encoding == "float":
                    code = "f" if fmt.storage_bits == 32 else "d"
                    endian = "<" if fmt.endianness == "little" else ">"
                    if any(not math.isfinite(sample[0]) for sample in struct.iter_unpack(endian + code, block)):
                        raise MediaValidationError("invalid_audio", "PCM payload contains a nonfinite float sample")
                if fmt.endianness == "big":
                    block = b"".join(
                        block[index:index + bytes_per_sample][::-1]
                        for index in range(0, len(block), bytes_per_sample)
                    )
                digest.update(block)
    return digest.hexdigest(), total_frames


def inspect_media_file(
    filepath: str | Path,
    *,
    ffprobe: Mapping[str, object] | None = None,
    raw_format: RawFormat | Mapping[str, object] | None = None,
    provider_format_evidence: str | None = None,
) -> MediaInspection:
    """Inspect a local file with bounded memory and return PCM data spans.

    `sample_spans` contains absolute file offsets and lengths. The caller keeps
    the original object intact and streams those ranges rather than gathering
    the samples into a second in-memory copy.
    """
    path = Path(filepath)
    if not path.is_absolute() or not path.is_file():
        raise MediaValidationError("invalid_media", "Media input must be an existing absolute local file")
    try:
        path = path.resolve(strict=True)
        initial_stat = path.stat()
        size = initial_stat.st_size
        if size <= 0:
            raise MediaValidationError("invalid_audio", "Media file must not be empty")
        with path.open("rb") as source:
            header = source.read(12)
        is_wave = len(header) >= 12 and header[8:12] == b"WAVE" and header[:4] in {b"RIFF", b"RIFX", b"RF64"}
        bytes_hash = _stream_sha256(path)
        if is_wave:
            fmt, container, spans = _wave_file_layout(path)
            if raw_format is not None:
                _assert_matches_raw_format(fmt, raw_format)
            if ffprobe is not None:
                _assert_pcm_matches_probe(fmt, ffprobe)
            sample_hash, frames = _hash_sample_spans(path, spans, fmt)
            media = MediaProperties.model_validate({
                "codec": _codec_name(fmt), "container": container,
                "sample_rate_hz": fmt.sample_rate_hz, "channels": fmt.channels,
                "encoding": fmt.encoding, "storage_bits": fmt.storage_bits,
                "valid_bits": fmt.valid_bits, "endianness": fmt.endianness,
                "bitrate_bps": None, "frame_count": str(frames),
                "duration_seconds": frames / fmt.sample_rate_hz,
                "canonical_sample_sha256": sample_hash,
            }, strict=True)
            inspected = MediaInspection(media, bytes_hash, size, None, True, spans)
            _ensure_file_unchanged(path, initial_stat)
            return inspected
        if raw_format is not None:
            if not isinstance(provider_format_evidence, str) or not provider_format_evidence.strip():
                raise MediaValidationError("invalid_media_metadata", "Headerless PCM requires saved provider format evidence")
            if ffprobe is not None and _parse_probe(ffprobe).encoding == "compressed":
                raise MediaValidationError("media_mismatch", "Raw PCM metadata cannot override detected compressed media")
            raw = _raw_pcm_format(raw_format)
            values = _format_values(raw_format)
            if values.get("provider_format_evidence") != provider_format_evidence:
                raise MediaValidationError("media_mismatch", "RawFormat evidence differs from the saved provider evidence")
            frame_bytes = raw.channels * raw.storage_bits // 8
            if size == 0 or size % frame_bytes:
                raise MediaValidationError("invalid_audio", "Raw PCM payload ends with a partial sample frame")
            span = ((0, size),)
            sample_hash, frames = _hash_sample_spans(path, span, raw)
            if ffprobe is not None:
                _assert_pcm_matches_probe(raw, ffprobe)
            media = MediaProperties.model_validate({
                "codec": _codec_name(raw), "container": "raw_pcm",
                "sample_rate_hz": raw.sample_rate_hz, "channels": raw.channels,
                "encoding": raw.encoding, "storage_bits": raw.storage_bits,
                "valid_bits": raw.valid_bits, "endianness": raw.endianness,
                "bitrate_bps": None, "frame_count": str(frames),
                "duration_seconds": frames / raw.sample_rate_hz,
                "canonical_sample_sha256": sample_hash,
            }, strict=True)
            inspected = MediaInspection(media, bytes_hash, size, None, True, span)
            _ensure_file_unchanged(path, initial_stat)
            return inspected
        if ffprobe is None:
            raise MediaValidationError("unsupported_media", "Headered non-WAVE audio requires actual ffprobe metadata")
        media = _parse_probe(ffprobe)
        inspected = MediaInspection(media, bytes_hash, size, None, False)
        _ensure_file_unchanged(path, initial_stat)
        return inspected
    except OSError as exc:
        raise MediaValidationError("invalid_media", f"Unable to inspect local media file: {exc}") from exc
