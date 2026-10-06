from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct

import pytest

from cognita.books.assembly import (
    AssemblyError,
    Mp3Source,
    PcmSource,
    _wave_header,
    assemble_pcm_stream,
    build_test_mp3_stream_copy_argv,
    plan_pcm_timeline,
    production_mp3_argv,
    verify_mp3_packet_copy,
    wrap_pcm_as_wave,
    write_ffconcat_manifest,
)
from cognita.books.media import MediaInspection, inspect_media_file
from cognita.books.jobs import PacketFact
from cognita.books.models import BuildMetadata, MediaProperties, ProductionTarget, RawFormat, SilenceGap


def _raw_format(*, endian: str = "little", rate: int = 8000, channels: int = 1, bits: int = 16) -> RawFormat:
    return RawFormat.model_validate({
        "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": rate,
        "channels": channels, "storage_bits": bits, "valid_bits": bits,
        "endianness": endian, "interleaving": "interleaved",
        "provider_format_evidence": f"provider exact format s{bits}{'le' if endian == 'little' else 'be'}",
    }, strict=True)


def _target(*, rate: int = 8000, channels: int = 1, bits: int = 16, bitrate: int = 192) -> ProductionTarget:
    return ProductionTarget.model_validate({
        "sample_rate_hz": rate, "channels": channels, "encoding": "signed_integer",
        "storage_bits": bits, "valid_bits": bits, "mp3_bitrate_kbps": bitrate,
    }, strict=True)


def _metadata() -> BuildMetadata:
    return BuildMetadata.model_validate({"title": "Synthetic", "author": "Test", "edition": "Unit"}, strict=True)


def _raw_source(tmp_path, source_id: str, samples: bytes, *, endian: str = "little", rate: int = 8000) -> PcmSource:
    path = tmp_path / f"{source_id}.pcm"
    path.write_bytes(samples)
    inspection = inspect_media_file(
        path, raw_format=_raw_format(endian=endian, rate=rate),
        provider_format_evidence=_raw_format(endian=endian, rate=rate).provider_format_evidence,
    )
    return PcmSource(source_id, path, inspection)


def _mp3_source(source_id: str, path, *, bitrate: int = 192000) -> Mp3Source:
    media = MediaProperties.model_validate({
        "codec": "mp3", "container": "mp3", "sample_rate_hz": 44100, "channels": 1,
        "encoding": "compressed", "storage_bits": None, "valid_bits": None,
        "endianness": "not_applicable", "bitrate_bps": bitrate, "frame_count": "10",
        "duration_seconds": 0.261, "canonical_sample_sha256": None,
    }, strict=True)
    return Mp3Source(source_id, path, MediaInspection(media, "a" * 64, 16, None, False))


def test_timeline_and_stream_are_sample_exact_with_explicit_frame_gap(tmp_path) -> None:
    first = _raw_source(tmp_path, "chunk-1", struct.pack("<hh", 10, -10))
    second = _raw_source(tmp_path, "chunk-2", struct.pack("<hh", 20, -20))
    gaps = [SilenceGap(before_id="chunk-2", sample_frames="3")]
    timeline = plan_pcm_timeline([first, second], gaps, _target())
    assert timeline.frame_count == "7"
    assert [(item.kind, item.start_frame, item.end_frame) for item in timeline.entries] == [
        ("audio", "0", "2"), ("silence", "2", "5"), ("audio", "5", "7"),
    ]
    output = io.BytesIO()
    result = assemble_pcm_stream([first, second], gaps, _target(), output)
    expected = struct.pack("<hh", 10, -10) + bytes(6) + struct.pack("<hh", 20, -20)
    assert output.getvalue() == expected
    assert result.sample_bytes == len(expected)
    assert result.bytes_sha256 == hashlib.sha256(expected).hexdigest()
    assert result.samples_sha256 == hashlib.sha256(expected).hexdigest()


def test_big_endian_native_pcm_is_sample_exactly_canonicalized(tmp_path) -> None:
    source = _raw_source(tmp_path, "big", struct.pack(">hh", 258, -2), endian="big")
    output = io.BytesIO()
    result = assemble_pcm_stream([source], [], _target(), output)
    assert output.getvalue() == struct.pack("<hh", 258, -2)
    assert result.samples_sha256 == hashlib.sha256(output.getvalue()).hexdigest()


def test_assembly_rejects_mismatch_stale_bytes_unknown_gap_and_duplicate_ids(tmp_path) -> None:
    source = _raw_source(tmp_path, "first", b"\x01\x00")
    with pytest.raises(AssemblyError, match="does not exactly match"):
        plan_pcm_timeline([source], [], _target(rate=16000))
    with pytest.raises(AssemblyError, match="reference an existing"):
        plan_pcm_timeline([source], [SilenceGap(before_id="missing", sample_frames="1")], _target())
    with pytest.raises(AssemblyError, match="unique nonempty"):
        plan_pcm_timeline([source, source], [], _target())
    source_path = source.filepath
    source_path.write_bytes(b"\x02\x00")
    with pytest.raises(AssemblyError, match="bytes changed"):
        assemble_pcm_stream([source], [], _target(), io.BytesIO())


def test_wave_wrapper_preserves_sample_bytes_and_reports_header_facts(tmp_path) -> None:
    source = _raw_source(tmp_path, "one", struct.pack("<hh", -1, 5))
    pcm_path = tmp_path / "master.pcm"
    output = io.BytesIO()
    assembled = assemble_pcm_stream([source], [], _target(), output)
    pcm_path.write_bytes(output.getvalue())
    wrapped = wrap_pcm_as_wave(pcm_path, tmp_path / "master.wav", _target(), assembled)
    parsed = inspect_media_file(wrapped.filepath)
    assert wrapped.container == "wav"
    assert parsed.media.canonical_sample_sha256 == assembled.samples_sha256
    assert parsed.media.frame_count == "2"
    assert parsed.sample_spans == ((44, len(output.getvalue())),)
    assert wrapped.bytes_sha256 == hashlib.sha256((tmp_path / "master.wav").read_bytes()).hexdigest()


def test_wave_header_uses_rf64_sizes_for_large_frame_counts() -> None:
    # Exercise 64-bit container arithmetic without allocating multi-gigabyte audio.
    frames = (1 << 31) + 1
    data_size = frames * 2
    header, container = _wave_header(_target(), frames)
    assert container == "rf64"
    assert header[:4] == b"RF64" and header[12:16] == b"ds64"
    riff_size, declared_data, declared_frames = struct.unpack_from("<QQQ", header, 20)
    assert declared_data == data_size and declared_frames == frames
    assert riff_size == 72 + data_size


def test_production_mp3_command_uses_one_native_pcm_encode_and_no_overwrite(tmp_path) -> None:
    pcm_path = tmp_path / "master.pcm"
    pcm_path.write_bytes(b"\x00\x00")
    argv = production_mp3_argv(tmp_path / "ffmpeg.exe", pcm_path, tmp_path / "book.mp3", _target(rate=44100), _metadata())
    assert argv[0] == str(tmp_path / "ffmpeg.exe")
    assert argv[argv.index("-f") + 1] == "s16le"
    assert argv[argv.index("-c:a") + 1] == "libmp3lame"
    assert argv[argv.index("-b:a") + 1] == "192k"
    assert argv[argv.index("-ar") + 1] == "44100"
    assert "-y" not in argv
    assert "title=Synthetic" in argv and "artist=Test" in argv


def test_mp3_stream_copy_is_explicitly_chapter_only_and_requires_matching_inputs(tmp_path) -> None:
    first_path = tmp_path / "first.mp3"
    second_path = tmp_path / "second.mp3"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = _mp3_source("first", first_path)
    second = _mp3_source("second", second_path)
    manifest = tmp_path / "inputs.ffconcat"
    write_ffconcat_manifest([first_path, second_path], manifest)
    argv = build_test_mp3_stream_copy_argv(
        tmp_path / "ffmpeg.exe", [first, second], [], scope="chapter", manifest=manifest,
        destination=tmp_path / "chapter-test.mp3", metadata=_metadata(),
    )
    assert argv[argv.index("-c:a") + 1] == "copy"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-format_whitelist") + 1] == "concat,mp3"
    with pytest.raises(AssemblyError, match="chapter tests only"):
        build_test_mp3_stream_copy_argv(
            tmp_path / "ffmpeg.exe", [first], [], scope="book", manifest=manifest,
            destination=tmp_path / "book-test.mp3", metadata=_metadata(),
        )
    with pytest.raises(AssemblyError, match="cannot represent explicit"):
        build_test_mp3_stream_copy_argv(
            tmp_path / "ffmpeg.exe", [first], [SilenceGap(before_id="first", sample_frames="1")],
            scope="chapter", manifest=manifest, destination=tmp_path / "gapped.mp3", metadata=_metadata(),
        )


def test_packet_copy_verification_compares_each_packet_hash_in_order() -> None:
    first = (PacketFact(123, "1" * 64), PacketFact(99, "2" * 64))
    second = (PacketFact(80, "3" * 64),)
    result = verify_mp3_packet_copy([first, second], [*first, *second])
    assert result.packet_count == 3
    assert len(result.ordered_packets_sha256) == 64
    with pytest.raises(AssemblyError, match="packet count differs"):
        verify_mp3_packet_copy([first], [first[0]])
    with pytest.raises(AssemblyError, match="packet 1 differs"):
        verify_mp3_packet_copy([first], [first[0], PacketFact(99, "f" * 64)])


def test_ffprobe_packet_reader_returns_bounded_ordered_hash_facts(tmp_path, monkeypatch) -> None:
    from cognita.books import jobs

    media_path = tmp_path / "test.mp3"
    media_path.write_bytes(b"synthetic packet fixture")
    packet_hash = "a" * 64

    async def fake_run_process(argv, **kwargs):
        assert argv[argv.index("-show_entries") + 1] == "packet=size,data_hash"
        assert argv[argv.index("-show_data_hash") + 1] == "sha256"
        assert kwargs["max_output_bytes"] == 16 * 1024 * 1024
        return jobs.ProcessResult(
            0,
            json.dumps({"packets": [{"size": "120", "data_hash": f"SHA256:{packet_hash}"}]}).encode(),
            b"", False, False, 0.01, False, False,
        )

    monkeypatch.setattr(jobs, "run_process", fake_run_process)
    facts = asyncio.run(jobs.ffprobe_packet_facts(tmp_path / "ffprobe", media_path))
    assert facts == (PacketFact(120, packet_hash),)


def test_ffconcat_manifest_quotes_local_paths_and_rejects_existing_destination(tmp_path) -> None:
    path = tmp_path / "chapter 'one'.mp3"
    path.write_bytes(b"sample")
    manifest = tmp_path / "input.ffconcat"
    write_ffconcat_manifest([path], manifest)
    content = manifest.read_text(encoding="utf-8")
    assert content.startswith("ffconcat version 1.0\nfile '")
    assert "'\\''one'\\''" in content
    with pytest.raises(AssemblyError, match="not already exist"):
        write_ffconcat_manifest([path], manifest)
