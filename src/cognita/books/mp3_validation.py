from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .jobs import ProcessRunnerError, run_process


_SHA256 = re.compile(r"^(?:SHA256:)?([0-9a-fA-F]{64})$")
_MAX_PROBE_OUTPUT = 32 * 1024 * 1024


@dataclass(frozen=True)
class Mp3Packet:
    position: int
    size_bytes: int
    data_sha256: str
    skip_samples: int
    discard_padding: int


@dataclass(frozen=True)
class Mp3DecodedFrame:
    packet_position: int
    decoded_start_frame: int
    decoded_end_frame: int


@dataclass(frozen=True)
class Mp3ToolInvocation:
    tool: str
    purpose: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Mp3DecoderFileFacts:
    filepath: str
    codec: str
    sample_rate: int
    channels: int
    decoded_frames: int
    decoded_duration_seconds: float
    packet_count: int
    encoder_delay_samples: int
    encoder_padding_samples: int
    packets: tuple[Mp3Packet, ...]
    frames: tuple[Mp3DecodedFrame, ...]


@dataclass(frozen=True)
class Mp3JoinBoundary:
    left_source_index: int
    right_source_index: int
    output_packet_index: int
    output_decoded_frame_offset: int
    source_decoded_frames_before_join: int
    decoded_offset_delta_frames: int
    left_encoder_padding_samples: int
    right_encoder_delay_samples: int


@dataclass(frozen=True)
class Mp3DecoderVerification:
    verification_version: str
    status: str
    packet_order_checked: bool
    decoder_checked: bool
    boundaries_checked: bool
    sources: tuple[Mp3DecoderFileFacts, ...]
    output: Mp3DecoderFileFacts
    join_boundaries: tuple[Mp3JoinBoundary, ...]
    invocations: tuple[Mp3ToolInvocation, ...]


class Mp3DecoderVerificationError(ProcessRunnerError):
    """A packet or decoder fact was missing, invalid, incompatible, or failed."""


def _fail(code: str, message: str) -> Mp3DecoderVerificationError:
    return Mp3DecoderVerificationError(code, message)


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _fail("tool_timeout", "MP3 decoder verification exceeded its total time limit")
    return remaining


def _local_mp3(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise _fail("invalid_media_path", "MP3 decoder verification requires existing absolute local files")
    if candidate.is_symlink():
        raise _fail("invalid_media_path", "MP3 decoder verification does not follow symlink inputs")
    return candidate


def _positive_int(value: object, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail("invalid_probe_facts", f"FFprobe returned invalid {label}")
    return value


async def _probe_file(
    executable: str | Path,
    filepath: Path,
    *,
    timeout_seconds: float,
    cancel_event,
) -> tuple[Mapping[str, object], tuple[Mp3Packet, ...], tuple[tuple[int, int], ...], tuple[str, ...]]:
    argv = [
        str(executable), "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", "mp3", "-select_streams", "a:0",
        "-show_streams", "-show_packets", "-show_frames",
        "-show_data_hash", "sha256", "-of", "json",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration,time_base,start_time,initial_padding,trailing_padding:"
        "packet=size,data_hash,pos,side_data_list:frame=pkt_pos,nb_samples",
        str(filepath),
    ]
    result = await run_process(
        argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=_MAX_PROBE_OUTPUT,
        cancel_event=cancel_event,
    )
    _check_process(result, "ffprobe")
    if result.stdout_truncated:
        raise _fail("probe_output_limit", "MP3 packet/frame facts exceeded the bounded probe output")
    try:
        document = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail("invalid_probe_output", "FFprobe did not return valid MP3 facts") from exc
    if not isinstance(document, dict):
        raise _fail("invalid_probe_output", "FFprobe MP3 facts must have an object root")
    streams = document.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise _fail("invalid_probe_output", "FFprobe must return exactly one selected audio stream")
    stream = streams[0]
    if stream.get("codec_name") != "mp3":
        raise _fail("unsupported_media", "Chapter stream-copy verification requires MP3 input and output")
    sample_rate = _positive_int(stream.get("sample_rate"), "sample rate")
    channels = _positive_int(stream.get("channels"), "channel count")
    if sample_rate > 384_000 or channels > 32:
        raise _fail("invalid_probe_facts", "FFprobe returned implausible MP3 audio dimensions")
    packets_value = document.get("packets")
    if not isinstance(packets_value, list) or not packets_value:
        raise _fail("invalid_probe_output", "FFprobe returned no MP3 packets")
    packets: list[Mp3Packet] = []
    packet_positions: set[int] = set()
    for item in packets_value:
        if not isinstance(item, Mapping):
            raise _fail("invalid_probe_output", "FFprobe packet entry must be an object")
        size = _positive_int(item.get("size"), "packet size")
        pos = _positive_int(item.get("pos"), "packet position", allow_zero=True)
        if pos in packet_positions:
            raise _fail("invalid_probe_facts", "FFprobe returned duplicate MP3 packet positions")
        packet_positions.add(pos)
        digest = item.get("data_hash")
        match = _SHA256.fullmatch(digest) if isinstance(digest, str) else None
        if match is None:
            raise _fail("invalid_probe_facts", "FFprobe packet is missing a SHA-256 data hash")
        skip = discard = 0
        side_data = item.get("side_data_list", [])
        if not isinstance(side_data, list):
            raise _fail("invalid_probe_facts", "FFprobe packet side data must be an array")
        for side in side_data:
            if not isinstance(side, Mapping):
                raise _fail("invalid_probe_facts", "FFprobe packet side-data entry must be an object")
            if side.get("side_data_type") == "Skip Samples":
                skip = _positive_int(side.get("skip_samples", 0), "encoder delay", allow_zero=True)
                discard = _positive_int(side.get("discard_padding", 0), "encoder padding", allow_zero=True)
        packets.append(Mp3Packet(pos, size, match.group(1).lower(), skip, discard))
    frames_value = document.get("frames")
    if not isinstance(frames_value, list) or not frames_value:
        raise _fail("invalid_probe_output", "FFprobe returned no decoded MP3 frames")
    frames: list[tuple[int, int]] = []
    frame_packet_indexes: list[int] = []
    packet_index_by_position = {packet.position: index for index, packet in enumerate(packets)}
    for item in frames_value:
        if not isinstance(item, Mapping):
            raise _fail("invalid_probe_output", "FFprobe decoded-frame entry must be an object")
        pos = _positive_int(item.get("pkt_pos"), "decoded-frame packet position", allow_zero=True)
        samples = _positive_int(item.get("nb_samples"), "decoded frame sample count")
        if pos not in packet_positions:
            raise _fail("invalid_probe_facts", "Decoded MP3 frame does not map to an input packet")
        frames.append((pos, samples))
        frame_packet_indexes.append(packet_index_by_position[pos])
    if any(current < previous for previous, current in zip(frame_packet_indexes, frame_packet_indexes[1:])):
        raise _fail("invalid_probe_facts", "Decoded MP3 frames do not follow packet order")
    return stream, tuple(packets), tuple(frames), tuple(argv)


def _check_process(result, tool: str) -> None:
    if result.cancelled:
        raise _fail("cancelled", f"{tool} was cancelled")
    if result.timed_out:
        raise _fail("tool_timeout", f"{tool} exceeded its time limit")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1_000:]
        raise _fail("decoder_failed" if tool == "ffmpeg" else "probe_failed", f"{tool} exited with status {result.returncode}: {message}")


def _file_facts(path: Path, stream: Mapping[str, object], packets: tuple[Mp3Packet, ...], frames: tuple[tuple[int, int], ...]) -> Mp3DecoderFileFacts:
    sample_rate = _positive_int(stream.get("sample_rate"), "sample rate")
    channels = _positive_int(stream.get("channels"), "channel count")
    decoded_frames = sum(samples for _, samples in frames)
    if decoded_frames <= 0 or decoded_frames > 2**63 - 1:
        raise _fail("invalid_probe_facts", "Decoded MP3 sample count is outside the supported range")
    duration = decoded_frames / sample_rate
    if not math.isfinite(duration):
        raise _fail("invalid_probe_facts", "Decoded MP3 duration is not finite")
    delay = sum(packet.skip_samples for packet in packets[:1])
    padding = sum(packet.discard_padding for packet in packets[-1:])
    frame_offset = 0
    decoded = []
    for packet_position, sample_count in frames:
        decoded.append(Mp3DecodedFrame(packet_position, frame_offset, frame_offset + sample_count))
        frame_offset += sample_count
    return Mp3DecoderFileFacts(
        str(path), str(stream["codec_name"]), sample_rate, channels, decoded_frames,
        duration, len(packets), delay, padding, packets, tuple(decoded),
    )


async def _decode_to_null(
    executable: str | Path,
    filepath: Path,
    *,
    timeout_seconds: float,
    cancel_event,
) -> tuple[str, ...]:
    argv = (
        str(executable), "-hide_banner", "-nostdin", "-v", "error",
        "-protocol_whitelist", "file", "-format_whitelist", "mp3",
        "-i", str(filepath), "-map", "0:a:0", "-f", "null", "-",
    )
    result = await run_process(
        argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=64 * 1024,
        cancel_event=cancel_event,
    )
    _check_process(result, "ffmpeg")
    if result.stdout_truncated or result.stderr_truncated:
        raise _fail("decoder_output_limit", "FFmpeg decoder emitted excessive diagnostic output")
    return argv


async def verify_chapter_mp3_decoder(
    ffmpeg_executable: str | Path,
    ffprobe_executable: str | Path,
    source_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    timeout_seconds: float = 300.0,
    cancel_event=None,
) -> Mp3DecoderVerification:
    """Verify ordered MP3 packets and independently measure decoder output and joins.

    FFmpeg decodes each source and the joined test output to its null muxer, so
    no native PCM/master or whole-audio in-memory buffer is created. FFprobe's
    decoded frame records provide sample counts and packet-to-frame join offsets.
    A successful result describes decoder facts; it does not assert seam quality.
    """
    if isinstance(source_paths, (str, bytes)) or not source_paths:
        raise _fail("invalid_source_list", "At least one source MP3 is required")
    sources = tuple(_local_mp3(path) for path in source_paths)
    output = _local_mp3(output_path)
    if len({path.resolve() for path in (*sources, output)}) != len(sources) + 1:
        raise _fail("invalid_source_list", "Output and ordered source MP3 paths must be distinct")
    timeout_seconds = float(timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise _fail("invalid_timeout", "Decoder verification timeout must be finite and positive")
    deadline = time.monotonic() + timeout_seconds

    per_source = []
    per_source_frames: list[tuple[tuple[int, int], ...]] = []
    source_packets: list[tuple[Mp3Packet, ...]] = []
    invocations: list[Mp3ToolInvocation] = []
    for path in sources:
        stream, packets, frames, probe_argv = await _probe_file(
            ffprobe_executable, path, timeout_seconds=_remaining_seconds(deadline), cancel_event=cancel_event,
        )
        invocations.append(Mp3ToolInvocation("ffprobe", f"source:{path.name}:packet_frame_facts", probe_argv))
        decode_argv = await _decode_to_null(
            ffmpeg_executable, path, timeout_seconds=_remaining_seconds(deadline), cancel_event=cancel_event,
        )
        invocations.append(Mp3ToolInvocation("ffmpeg", f"source:{path.name}:decode_to_null", decode_argv))
        per_source.append(_file_facts(path, stream, packets, frames))
        per_source_frames.append(frames)
        source_packets.append(packets)

    output_stream, output_packets, output_frames, output_probe_argv = await _probe_file(
        ffprobe_executable, output, timeout_seconds=_remaining_seconds(deadline), cancel_event=cancel_event,
    )
    invocations.append(Mp3ToolInvocation("ffprobe", "output:packet_frame_facts", output_probe_argv))
    output_argv = await _decode_to_null(
        ffmpeg_executable, output, timeout_seconds=_remaining_seconds(deadline), cancel_event=cancel_event,
    )
    invocations.append(Mp3ToolInvocation("ffmpeg", "output:decode_to_null", output_argv))
    output_facts = _file_facts(output, output_stream, output_packets, output_frames)
    if any((fact.sample_rate, fact.channels) != (output_facts.sample_rate, output_facts.channels) for fact in per_source):
        raise _fail("media_mismatch", "Source and output MP3 decoder formats differ")

    expected_packets = tuple(packet for group in source_packets for packet in group)
    if len(expected_packets) != len(output_packets) or any(
        (left.size_bytes, left.data_sha256) != (right.size_bytes, right.data_sha256)
        for left, right in zip(expected_packets, output_packets)
    ):
        raise _fail("packet_mismatch", "Joined MP3 packets differ from ordered source packets")
    if (
        output_packets[0].skip_samples != source_packets[0][0].skip_samples
        or output_packets[-1].discard_padding != source_packets[-1][-1].discard_padding
    ):
        raise _fail("delay_padding_mismatch", "Joined MP3 start delay or end padding facts differ from the ordered sources")

    byte_offset = 0
    for packet in output_packets:
        if packet.position != byte_offset:
            # Some demuxers report leading ID3 bytes, which are not audio packet
            # bytes. Keep exact mappings by using packet positions, not assuming 0.
            if packet.position < byte_offset:
                raise _fail("invalid_probe_facts", "Output MP3 packet positions overlap or move backwards")
            byte_offset = packet.position
        byte_offset = packet.position + packet.size_bytes

    boundaries: list[Mp3JoinBoundary] = []
    packet_index = 0
    source_decoded_before = 0
    for index, group in enumerate(source_packets[:-1]):
        packet_index += len(group)
        next_packet_position = output_packets[packet_index].position
        decoded_offset = 0
        reached = False
        for packet_pos, samples in output_frames:
            if packet_pos >= next_packet_position:
                reached = True
                break
            decoded_offset += samples
        if not reached:
            raise _fail("unmappable_join_boundary", "FFprobe decoded frames do not reach an internal source join")
        left_facts = per_source[index]
        right_facts = per_source[index + 1]
        boundaries.append(Mp3JoinBoundary(
            index, index + 1, packet_index, decoded_offset, source_decoded_before + left_facts.decoded_frames,
            decoded_offset - (source_decoded_before + left_facts.decoded_frames),
            left_facts.encoder_padding_samples, right_facts.encoder_delay_samples,
        ))
        source_decoded_before += left_facts.decoded_frames

    return Mp3DecoderVerification(
        "mp3-decoder-v1", "checked", True, True, True,
        tuple(per_source), output_facts, tuple(boundaries), tuple(invocations),
    )


__all__ = [
    "Mp3DecoderFileFacts", "Mp3DecoderVerification", "Mp3DecoderVerificationError",
    "Mp3DecodedFrame", "Mp3JoinBoundary", "Mp3Packet", "Mp3ToolInvocation",
    "verify_chapter_mp3_decoder",
]
