"""Sample-exact PCM timelines and bounded audio assembly primitives.

These helpers do not choose take order, mutate durable book state, or create
jobs. Callers provide an explicit order and gaps and stage new output files.
Native samples are copied as canonical little-endian PCM with no gain,
resampling, trimming, or other signal processing.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Sequence

from .media import MediaInspection
from .jobs import PacketFact
from .models import BuildMetadata, MediaProperties, ProductionTarget, SilenceGap
from pydantic.experimental.missing_sentinel import MISSING


_UINT64_MAX = (1 << 64) - 1
_BLOCK_BYTES = 1024 * 1024


class AssemblyError(ValueError):
    """An input cannot be assembled without changing its audio semantics."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PcmSource:
    """One explicit ordered native PCM source in the assembly plan."""

    source_id: str
    filepath: str | Path
    inspection: MediaInspection


@dataclass(frozen=True)
class TimelineEntry:
    source_id: str
    kind: str
    start_frame: str
    end_frame: str
    source_bytes_sha256: str | None
    source_samples_sha256: str | None


@dataclass(frozen=True)
class PcmTimeline:
    sample_rate_hz: int
    channels: int
    encoding: str
    storage_bits: int
    frame_count: str
    entries: tuple[TimelineEntry, ...]


@dataclass(frozen=True)
class PcmAssemblyResult:
    frame_count: str
    sample_bytes: int
    bytes_sha256: str
    samples_sha256: str
    timeline: PcmTimeline


@dataclass(frozen=True)
class WaveWrapperResult:
    filepath: str
    container: str
    size_bytes: int
    bytes_sha256: str
    media: MediaProperties


@dataclass(frozen=True)
class PacketCopyValidation:
    packet_count: int
    ordered_packets_sha256: str


@dataclass(frozen=True)
class Mp3Source:
    source_id: str
    filepath: str | Path
    inspection: MediaInspection


def _fail(code: str, message: str) -> None:
    raise AssemblyError(code, message)


def _frame_count(value: object) -> int:
    if isinstance(value, bool):
        _fail("invalid_media_metadata", "PCM frame count must be a decimal integer")
    if isinstance(value, int):
        frames = value
    elif isinstance(value, str) and value.isdecimal():
        frames = int(value)
    else:
        _fail("invalid_media_metadata", "PCM frame count must be a decimal integer")
    if frames < 0 or frames > _UINT64_MAX:
        _fail("unsupported_large_master", "PCM frame count exceeds the supported 64-bit timeline")
    return frames


def _target_values(target: ProductionTarget) -> tuple[int, int, str, int]:
    if not isinstance(target, ProductionTarget):
        _fail("invalid_media_metadata", "target must be a validated ProductionTarget")
    return target.sample_rate_hz, target.channels, target.encoding, target.storage_bits


def _source_path(source: PcmSource) -> Path:
    path = Path(source.filepath)
    if not path.is_absolute() or not path.is_file():
        _fail("invalid_media", f"PCM source {source.source_id!r} must be an existing absolute local file")
    return path.resolve(strict=True)


def _source_frames(source: PcmSource, target: ProductionTarget) -> int:
    media = source.inspection.media
    target_rate, target_channels, target_encoding, target_bits = _target_values(target)
    if not source.inspection.native_pcm or media.encoding not in {"signed_integer", "float"}:
        _fail("unsupported_media", f"Source {source.source_id!r} is not verified native PCM")
    if (
        media.sample_rate_hz != target_rate
        or media.channels != target_channels
        or media.encoding != target_encoding
        or media.storage_bits != target_bits
        or media.valid_bits != target.valid_bits
    ):
        _fail("media_mismatch", f"Source {source.source_id!r} does not exactly match the production target")
    if media.canonical_sample_sha256 is None or media.endianness not in {"little", "big"}:
        _fail("invalid_media_metadata", f"Source {source.source_id!r} lacks verified PCM facts")
    frames = _frame_count(media.frame_count)
    frame_bytes = target_channels * target_bits // 8
    if frames == 0 or not source.inspection.sample_spans:
        _fail("invalid_audio", f"Source {source.source_id!r} has no file-backed PCM samples")
    span_frames = 0
    size = source.inspection.size_bytes
    previous_end = -1
    for start, length in source.inspection.sample_spans:
        if (
            isinstance(start, bool) or isinstance(length, bool)
            or not isinstance(start, int) or not isinstance(length, int)
            or start < 0 or length <= 0 or start + length > size
            or start < previous_end or length % frame_bytes
        ):
            _fail("invalid_media_metadata", f"Source {source.source_id!r} has invalid sample spans")
        span_frames += length // frame_bytes
        previous_end = start + length
    if span_frames != frames:
        _fail("invalid_media_metadata", f"Source {source.source_id!r} frame count conflicts with its sample spans")
    _source_path(source)
    return frames


def _gap_map(gaps: Iterable[SilenceGap], source_ids: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for gap in gaps:
        if not isinstance(gap, SilenceGap):
            _fail("invalid_media_metadata", "gaps must contain validated SilenceGap objects")
        before_id = gap.before_id
        if not isinstance(before_id, str) or not before_id or before_id not in source_ids:
            _fail("invalid_media_metadata", "Every silence gap must reference an existing source ID")
        if before_id in result:
            _fail("invalid_media_metadata", f"Only one explicit gap may precede {before_id!r}")
        frames = _frame_count(gap.sample_frames)
        if frames == 0:
            _fail("invalid_media_metadata", "Explicit silence gaps must contain at least one sample frame")
        result[before_id] = frames
    return result


def plan_pcm_timeline(
    sources: Sequence[PcmSource],
    gaps: Iterable[SilenceGap],
    target: ProductionTarget,
) -> PcmTimeline:
    """Validate exact source compatibility/order and calculate explicit frame offsets."""
    if not sources:
        _fail("invalid_media_metadata", "An assembly requires at least one PCM source")
    if isinstance(sources, (str, bytes)):
        _fail("invalid_media_metadata", "sources must be an explicit sequence")
    if any(not isinstance(source, PcmSource) for source in sources):
        _fail("invalid_media_metadata", "sources must contain PcmSource objects")
    ids = [source.source_id for source in sources]
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        _fail("invalid_media_metadata", "PCM source IDs must be unique nonempty strings")
    pending_gaps = _gap_map(gaps, set(ids))
    rate, channels, encoding, bits = _target_values(target)
    cursor = 0
    entries: list[TimelineEntry] = []
    for source in sources:
        gap_frames = pending_gaps.pop(source.source_id, 0)
        if gap_frames:
            end = cursor + gap_frames
            if end > _UINT64_MAX:
                _fail("unsupported_large_master", "PCM timeline exceeds the supported 64-bit frame count")
            entries.append(TimelineEntry(source.source_id, "silence", str(cursor), str(end), None, None))
            cursor = end
        frames = _source_frames(source, target)
        end = cursor + frames
        if end > _UINT64_MAX:
            _fail("unsupported_large_master", "PCM timeline exceeds the supported 64-bit frame count")
        entries.append(TimelineEntry(
            source.source_id, "audio", str(cursor), str(end),
            source.inspection.bytes_sha256, source.inspection.media.canonical_sample_sha256,
        ))
        cursor = end
    if pending_gaps:
        _fail("invalid_media_metadata", "A silence gap was not placed in the explicit source order")
    return PcmTimeline(rate, channels, encoding, bits, str(cursor), tuple(entries))


def _write_all(output: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = output.write(view)
        if written is None or written <= 0:
            _fail("output_write_failed", "PCM output stopped accepting bytes")
        view = view[written:]


def _canonicalize_samples(raw: bytes, *, endianness: str, bytes_per_sample: int) -> bytes:
    if endianness == "little":
        return raw
    if endianness != "big" or len(raw) % bytes_per_sample:
        _fail("invalid_media_metadata", "PCM sample byte order or sample boundary is invalid")
    return b"".join(
        raw[index:index + bytes_per_sample][::-1]
        for index in range(0, len(raw), bytes_per_sample)
    )


def _source_sample_blocks(source: PcmSource, target: ProductionTarget):
    """Yield canonical samples and verify both original-byte and sample hashes."""
    path = _source_path(source)
    initial = path.stat()
    if initial.st_size != source.inspection.size_bytes:
        _fail("stale_media", f"Source {source.source_id!r} size changed after inspection")
    byte_hash = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_BLOCK_BYTES):
            byte_hash.update(block)
    if byte_hash.hexdigest() != source.inspection.bytes_sha256:
        _fail("stale_media", f"Source {source.source_id!r} bytes changed after inspection")
    sample_hash = hashlib.sha256()
    bytes_per_sample = target.storage_bits // 8
    frame_bytes = target.channels * bytes_per_sample
    with path.open("rb") as handle:
        for start, length in source.inspection.sample_spans:
            handle.seek(start)
            remaining = length
            while remaining:
                count = min(_BLOCK_BYTES, remaining)
                count -= count % frame_bytes
                if count == 0:
                    count = min(frame_bytes, remaining)
                block = handle.read(count)
                if len(block) != count:
                    _fail("stale_media", f"Source {source.source_id!r} sample data was truncated")
                canonical = _canonicalize_samples(
                    block, endianness=source.inspection.media.endianness,
                    bytes_per_sample=bytes_per_sample,
                )
                sample_hash.update(canonical)
                yield canonical
                remaining -= count
    after = path.stat()
    if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        _fail("stale_media", f"Source {source.source_id!r} changed during assembly")
    if sample_hash.hexdigest() != source.inspection.media.canonical_sample_sha256:
        _fail("stale_media", f"Source {source.source_id!r} sample hash changed after inspection")


def assemble_pcm_stream(
    sources: Sequence[PcmSource],
    gaps: Iterable[SilenceGap],
    target: ProductionTarget,
    output: BinaryIO,
) -> PcmAssemblyResult:
    """Stream one canonical little-endian PCM master, inserting only declared whole-frame gaps.

    The caller must supply a newly staged output stream: an input error can
    occur after earlier verified sources have already been written.
    """
    timeline = plan_pcm_timeline(sources, gaps, target)
    entries = iter(timeline.entries)
    content_hash = hashlib.sha256()
    sample_hash = hashlib.sha256()
    total_bytes = 0
    for entry in entries:
        if entry.kind == "silence":
            frames = _frame_count(entry.end_frame) - _frame_count(entry.start_frame)
            remaining = frames * target.channels * (target.storage_bits // 8)
            zero = bytes(min(_BLOCK_BYTES, max(target.channels * (target.storage_bits // 8), 1)))
            zero = zero[:len(zero) - (len(zero) % (target.channels * (target.storage_bits // 8)))]
            if not zero:
                _fail("invalid_media_metadata", "Target sample frame has invalid byte width")
            while remaining:
                block = zero[:min(len(zero), remaining)]
                _write_all(output, block)
                content_hash.update(block)
                sample_hash.update(block)
                total_bytes += len(block)
                remaining -= len(block)
            continue
        source = next(item for item in sources if item.source_id == entry.source_id)
        for block in _source_sample_blocks(source, target):
            _write_all(output, block)
            content_hash.update(block)
            sample_hash.update(block)
            total_bytes += len(block)
    expected = _frame_count(timeline.frame_count) * target.channels * (target.storage_bits // 8)
    if total_bytes != expected:
        _fail("invalid_media_metadata", "Assembled byte count does not match the planned frame count")
    return PcmAssemblyResult(
        timeline.frame_count, total_bytes, content_hash.hexdigest(), sample_hash.hexdigest(), timeline,
    )


def _wave_format_chunk(target: ProductionTarget) -> bytes:
    if target.channels > 2:
        _fail("unsupported_media", "WAVE wrapper v1 supports mono and stereo targets only")
    bytes_per_sample = target.storage_bits // 8
    block_align = target.channels * bytes_per_sample
    byte_rate = target.sample_rate_hz * block_align
    if byte_rate > 0xFFFFFFFF or block_align > 0xFFFF:
        _fail("unsupported_large_master", "WAVE target byte rate exceeds its header fields")
    format_code = 3 if target.encoding == "float" else 1
    return struct.pack(
        "<HHIIHH", format_code, target.channels, target.sample_rate_hz,
        byte_rate, block_align, target.storage_bits,
    )


def _wave_header(target: ProductionTarget, frame_count: int) -> tuple[bytes, str]:
    """Construct a RIFF/RF64 header without allocating the represented audio."""
    frame_bytes = target.channels * target.storage_bits // 8
    data_size = frame_count * frame_bytes
    fmt_chunk = _wave_format_chunk(target)
    riff_size32 = 36 + data_size + (data_size & 1)
    if riff_size32 > 0xFFFFFFFF:
        # Header is 80 bytes through the data payload; RIFF size excludes the
        # initial 8 bytes and includes the final even-byte pad when present.
        riff_size64 = 72 + data_size + (data_size & 1)
        header = (
            b"RF64\xff\xff\xff\xffWAVE"
            + b"ds64" + struct.pack("<IQQQI", 28, riff_size64, data_size, frame_count, 0)
            + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
            + b"data" + struct.pack("<I", 0xFFFFFFFF)
        )
        return header, "rf64"
    header = (
        b"RIFF" + struct.pack("<I", riff_size32) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
        + b"data" + struct.pack("<I", data_size)
    )
    return header, "wav"


def wrap_pcm_as_wave(
    pcm_filepath: str | Path,
    destination: str | Path,
    target: ProductionTarget,
    assembly: PcmAssemblyResult,
) -> WaveWrapperResult:
    """Write a byte-preserving RIFF or RF64 WAVE wrapper around a canonical PCM master."""
    source = Path(pcm_filepath)
    output_path = Path(destination)
    if not source.is_absolute() or not source.is_file() or not output_path.is_absolute():
        _fail("invalid_media", "PCM and WAVE output paths must be absolute local paths")
    if source.resolve(strict=True) == output_path.resolve(strict=False):
        _fail("invalid_media", "WAVE wrapper destination must differ from its PCM source")
    if output_path.exists():
        _fail("output_exists", "WAVE wrapper destination already exists")
    frame_count = _frame_count(assembly.frame_count)
    frame_bytes = target.channels * target.storage_bits // 8
    data_size = frame_count * frame_bytes
    if data_size != assembly.sample_bytes:
        _fail("invalid_media_metadata", "Assembly sample bytes conflict with target frame dimensions")
    header, container = _wave_header(target, frame_count)
    try:
        with source.open("rb") as raw, output_path.open("xb") as out:
            source_size = source.stat().st_size
            if source_size != data_size:
                _fail("stale_media", "PCM master size does not match its assembly facts")
            source_hash = hashlib.sha256()
            while block := raw.read(_BLOCK_BYTES):
                source_hash.update(block)
            if source_hash.hexdigest() != assembly.bytes_sha256:
                _fail("stale_media", "PCM master bytes changed after assembly")
            raw.seek(0)
            _write_all(out, header)
            sample_hash = hashlib.sha256()
            while block := raw.read(_BLOCK_BYTES):
                _write_all(out, block)
                sample_hash.update(block)
            if sample_hash.hexdigest() != assembly.samples_sha256:
                _fail("stale_media", "PCM master sample hash changed after assembly")
            if data_size & 1:
                _write_all(out, b"\x00")
            out.flush()
            os.fsync(out.fileno())
        wrapper_hash = hashlib.sha256()
        wrapper_size = 0
        with output_path.open("rb") as wrapped:
            while block := wrapped.read(_BLOCK_BYTES):
                wrapper_hash.update(block)
                wrapper_size += len(block)
        media = MediaProperties.model_validate({
            "codec": _pcm_codec(target), "container": container,
            "sample_rate_hz": target.sample_rate_hz, "channels": target.channels,
            "encoding": target.encoding, "storage_bits": target.storage_bits,
            "valid_bits": target.valid_bits, "endianness": "little", "bitrate_bps": None,
            "frame_count": str(frame_count), "duration_seconds": frame_count / target.sample_rate_hz,
            "canonical_sample_sha256": assembly.samples_sha256,
        }, strict=True)
        return WaveWrapperResult(str(output_path), container, wrapper_size, wrapper_hash.hexdigest(), media)
    except OSError as exc:
        raise AssemblyError("output_write_failed", f"Unable to write WAVE wrapper: {exc}") from exc


def _pcm_codec(target: ProductionTarget) -> str:
    prefix = "f" if target.encoding == "float" else "s"
    return f"pcm_{prefix}{target.storage_bits}le"


def _validate_metadata(metadata: BuildMetadata) -> list[str]:
    if not isinstance(metadata, BuildMetadata):
        _fail("invalid_media_metadata", "metadata must be a validated BuildMetadata object")
    arguments: list[str] = []
    for name, value in (("title", metadata.title), ("artist", metadata.author), ("album", metadata.edition)):
        arguments.extend(["-metadata", f"{name}={value}"])
    if metadata.chapter_number is not MISSING:
        arguments.extend(["-metadata", f"track={metadata.chapter_number}"])
    return arguments


def production_mp3_argv(
    executable: str | Path,
    pcm_filepath: str | Path,
    destination: str | Path,
    target: ProductionTarget,
    metadata: BuildMetadata,
) -> list[str]:
    """Build an explicit one-pass MP3 encode command from a verified PCM master."""
    pcm = Path(pcm_filepath)
    output = Path(destination)
    if not Path(executable).is_absolute() or not pcm.is_absolute() or not output.is_absolute():
        _fail("invalid_process_request", "FFmpeg and media paths must be absolute local paths")
    if not pcm.is_file():
        _fail("invalid_media", "PCM master must exist before MP3 encoding")
    if output.exists():
        _fail("output_exists", "MP3 destination already exists")
    rate, channels, _, _ = _target_values(target)
    if rate not in {32000, 44100, 48000} or target.mp3_bitrate_kbps not in {32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320}:
        _fail("unsupported_media", "Configured MP3 sample rate or bitrate is unsupported without resampling")
    if channels > 2:
        _fail("unsupported_media", "MP3 production supports mono or stereo targets only")
    sample_format = ("f" if target.encoding == "float" else "s") + str(target.storage_bits) + "le"
    return [
        str(executable), "-nostdin", "-hide_banner", "-v", "error",
        "-protocol_whitelist", "file", "-f", sample_format, "-ar", str(rate),
        "-ac", str(channels), "-i", str(pcm),
        "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame",
        "-b:a", f"{target.mp3_bitrate_kbps}k", "-ar", str(rate), "-ac", str(channels),
        "-id3v2_version", "3", *_validate_metadata(metadata), str(output),
    ]


def _validate_test_mp3_sources(sources: Sequence[Mp3Source], gaps: Iterable[SilenceGap], scope: str) -> None:
    if scope != "chapter":
        _fail("unsupported_build_mode", "MP3 stream-copy is available for chapter tests only")
    if tuple(gaps):
        _fail("unsupported_build_mode", "MP3 stream-copy cannot represent explicit PCM silence gaps")
    if not sources:
        _fail("invalid_media_metadata", "MP3 stream-copy requires at least one input")
    ids = [source.source_id for source in sources]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        _fail("invalid_media_metadata", "MP3 source IDs must be unique and nonempty")
    if any(source.inspection.native_pcm for source in sources):
        _fail("unsupported_build_mode", "MP3 stream-copy accepts compressed MP3 sources only")
    first = sources[0].inspection.media
    if (
        first.encoding != "compressed" or first.codec != "mp3"
        or first.bitrate_bps is None or first.bitrate_bps <= 0
    ):
        _fail("unsupported_media", "MP3 stream-copy requires verified MP3 codec and bitrate facts")
    for source in sources:
        media = source.inspection.media
        if (
            media.encoding != "compressed" or media.codec != "mp3"
            or media.sample_rate_hz != first.sample_rate_hz
            or media.channels != first.channels or media.bitrate_bps != first.bitrate_bps
        ):
            _fail("media_mismatch", "MP3 stream-copy inputs must have matching codec, rate, channels, and bitrate")


def write_ffconcat_manifest(paths: Sequence[str | Path], manifest: str | Path, *,
                            on_created: Callable[[Path, int], None] | None = None) -> None:
    """Write an ffconcat manifest containing only existing absolute local files."""
    manifest_path = Path(manifest)
    if not manifest_path.is_absolute() or manifest_path.exists():
        _fail("invalid_process_request", "Manifest path must be absolute and not already exist")
    if not paths:
        _fail("invalid_process_request", "An ffconcat manifest requires at least one media path")
    lines = ["ffconcat version 1.0"]
    for item in paths:
        path = Path(item)
        if not path.is_absolute() or not path.is_file():
            _fail("invalid_media", "Every MP3 stream-copy input must be an existing absolute local file")
        resolved = str(path.resolve(strict=True))
        if any(ord(character) < 32 for character in resolved):
            _fail("invalid_media", "Media paths cannot contain control characters")
        # FFmpeg's parser cannot escape a quote while remaining inside a
        # single-quoted token; close it, escape the quote, then reopen it.
        escaped = resolved.replace("\\", "\\\\").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            if on_created is not None:
                on_created(manifest_path, stream.fileno())
            stream.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise AssemblyError("output_write_failed", f"Unable to write FFmpeg concat manifest: {exc}") from exc


def build_test_mp3_stream_copy_argv(
    executable: str | Path,
    sources: Sequence[Mp3Source],
    gaps: Iterable[SilenceGap],
    *,
    scope: str,
    manifest: str | Path,
    destination: str | Path,
    metadata: BuildMetadata,
) -> list[str]:
    """Build the explicitly chapter-only packet-copy command; it makes no PCM claim."""
    _validate_test_mp3_sources(sources, gaps, scope)
    executable_path = Path(executable)
    manifest_path = Path(manifest)
    output = Path(destination)
    if not executable_path.is_absolute() or not manifest_path.is_absolute() or not output.is_absolute():
        _fail("invalid_process_request", "FFmpeg, manifest, and output paths must be absolute")
    if output.exists():
        _fail("output_exists", "MP3 stream-copy destination already exists")
    metadata_args = _validate_metadata(metadata)
    return [
        str(executable_path), "-nostdin", "-hide_banner", "-v", "error",
        "-f", "concat", "-safe", "0", "-protocol_whitelist", "file",
        "-format_whitelist", "concat,mp3", "-i", str(manifest_path),
        "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "copy", "-write_xing", "0",
        "-id3v2_version", "3", *metadata_args, str(output),
    ]


def verify_mp3_packet_copy(
    source_packet_sequences: Sequence[Sequence[PacketFact]],
    output_packets: Sequence[PacketFact],
) -> PacketCopyValidation:
    """Require the output packet byte hashes to equal inputs in explicit order."""
    expected = tuple(packet for sequence in source_packet_sequences for packet in sequence)
    actual = tuple(output_packets)
    if not expected or not actual or any(not sequence for sequence in source_packet_sequences):
        _fail("sample_verification_failed", "Packet-copy proof requires nonempty source and output packet lists")
    if len(expected) != len(actual):
        _fail("sample_verification_failed", "Stream-copy output packet count differs from its ordered inputs")
    digest = hashlib.sha256()
    for index, (source, copied) in enumerate(zip(expected, actual, strict=True)):
        for fact in (source, copied):
            if (
                not isinstance(fact, PacketFact)
                or isinstance(fact.size_bytes, bool)
                or not isinstance(fact.size_bytes, int)
                or not 0 < fact.size_bytes <= _UINT64_MAX
                or not isinstance(fact.data_sha256, str)
                or len(fact.data_sha256) != 64
                or any(character not in "0123456789abcdef" for character in fact.data_sha256)
            ):
                _fail("invalid_media_metadata", "Packet proof contains malformed size or hash facts")
        if source != copied:
            _fail("sample_verification_failed", f"Stream-copy packet {index} differs from its ordered source")
        digest.update(source.size_bytes.to_bytes(8, "big", signed=False))
        digest.update(bytes.fromhex(source.data_sha256))
    return PacketCopyValidation(len(expected), digest.hexdigest())


__all__ = [
    "AssemblyError", "Mp3Source", "PacketCopyValidation", "PcmAssemblyResult", "PcmSource", "PcmTimeline",
    "TimelineEntry", "WaveWrapperResult", "assemble_pcm_stream", "plan_pcm_timeline",
    "build_test_mp3_stream_copy_argv", "production_mp3_argv", "verify_mp3_packet_copy",
    "wrap_pcm_as_wave", "write_ffconcat_manifest",
]
