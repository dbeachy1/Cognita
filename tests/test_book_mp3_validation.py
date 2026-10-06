from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cognita.books.jobs import ProcessResult
from cognita.books.mp3_validation import Mp3DecoderVerificationError, verify_chapter_mp3_decoder


def _probe(*, packets, frames, sample_rate=44_100, channels=1):
    return {
        "streams": [{
            "codec_name": "mp3", "sample_rate": str(sample_rate), "channels": channels,
            "duration": "0.052244", "time_base": "1/14112000", "start_time": "0.025057",
        }],
        "packets": packets,
        "frames": frames,
    }


def _packet(pos, size, digest, *, skip=0, discard=0):
    side = []
    if skip or discard:
        side.append({"side_data_type": "Skip Samples", "skip_samples": skip, "discard_padding": discard})
    value = {"pos": str(pos), "size": str(size), "data_hash": f"SHA256:{digest}"}
    if side:
        value["side_data_list"] = side
    return value


@pytest.mark.asyncio
async def test_decoder_verification_separates_packet_equality_from_join_facts(tmp_path: Path, monkeypatch) -> None:
    sources = [tmp_path / "one.mp3", tmp_path / "two.mp3"]
    output = tmp_path / "joined.mp3"
    for path in (*sources, output):
        path.write_bytes(b"synthetic mp3")
    docs = {
        str(sources[0]): _probe(
            packets=[_packet(0, 2, "a" * 64, skip=20, discard=4)],
            frames=[{"pkt_pos": "0", "nb_samples": 1_100}],
        ),
        str(sources[1]): _probe(
            packets=[_packet(0, 3, "b" * 64, skip=12, discard=8)],
            frames=[{"pkt_pos": "0", "nb_samples": 1_140}],
        ),
        str(output): _probe(
            packets=[_packet(0, 2, "a" * 64, skip=20), _packet(2, 3, "b" * 64, discard=8)],
            frames=[{"pkt_pos": "0", "nb_samples": 1_100}, {"pkt_pos": "2", "nb_samples": 1_152}],
        ),
    }
    calls = []

    async def fake_run_process(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[0] == str(tmp_path / "ffprobe"):
            path = str(argv[-1])
            return ProcessResult(0, json.dumps(docs[path]).encode(), b"", False, False, .01, False, False)
        assert argv[0] == str(tmp_path / "ffmpeg")
        assert tuple(argv[-3:]) == ("-f", "null", "-")
        assert "-protocol_whitelist" in argv
        assert argv[argv.index("-protocol_whitelist") + 1] == "file"
        return ProcessResult(0, b"", b"", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", fake_run_process)
    facts = await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", sources, output)

    assert facts.status == "checked"
    assert facts.packet_order_checked and facts.decoder_checked and facts.boundaries_checked
    assert [source.decoded_frames for source in facts.sources] == [1_100, 1_140]
    assert facts.output.decoded_frames == 2_252
    assert facts.sources[0].encoder_delay_samples == 20
    assert facts.sources[0].encoder_padding_samples == 4
    assert facts.output.packets[0].skip_samples == 20
    assert facts.output.frames[1].decoded_start_frame == 1_100
    assert len(facts.invocations) == 6
    boundary = facts.join_boundaries[0]
    assert boundary.output_packet_index == 1
    assert boundary.output_decoded_frame_offset == 1_100
    assert boundary.decoded_offset_delta_frames == 0
    assert boundary.left_encoder_padding_samples == 4
    assert boundary.right_encoder_delay_samples == 12
    assert len([call for call in calls if call[0] == str(tmp_path / "ffmpeg")]) == 3


@pytest.mark.asyncio
async def test_decoder_verification_reports_nonzero_join_delta_without_claiming_seam_quality(tmp_path: Path, monkeypatch) -> None:
    first, second, output = (tmp_path / name for name in ("a.mp3", "b.mp3", "out.mp3"))
    for path in (first, second, output):
        path.write_bytes(b"fixture")
    docs = {
        str(first): _probe(packets=[_packet(0, 1, "a" * 64)], frames=[{"pkt_pos": "0", "nb_samples": 1_152}]),
        str(second): _probe(packets=[_packet(0, 1, "b" * 64)], frames=[{"pkt_pos": "0", "nb_samples": 1_152}]),
        str(output): _probe(
            packets=[_packet(0, 1, "a" * 64), _packet(1, 1, "b" * 64)],
            frames=[{"pkt_pos": "0", "nb_samples": 1_096}, {"pkt_pos": "1", "nb_samples": 1_080}],
        ),
    }

    async def fake_run_process(argv, **kwargs):
        if argv[0] == str(tmp_path / "ffprobe"):
            return ProcessResult(0, json.dumps(docs[str(argv[-1])]).encode(), b"", False, False, .01, False, False)
        return ProcessResult(0, b"", b"", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", fake_run_process)
    facts = await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", (first, second), output)
    assert facts.join_boundaries[0].decoded_offset_delta_frames == -56
    assert facts.output.decoded_frames == 2_176


@pytest.mark.asyncio
async def test_decoder_failure_or_packet_mismatch_fails_closed(tmp_path: Path, monkeypatch) -> None:
    first, output = tmp_path / "a.mp3", tmp_path / "out.mp3"
    first.write_bytes(b"fixture")
    output.write_bytes(b"fixture")
    doc = _probe(packets=[_packet(0, 1, "a" * 64)], frames=[{"pkt_pos": "0", "nb_samples": 1_152}])

    async def mismatched(argv, **kwargs):
        if argv[0] == str(tmp_path / "ffprobe"):
            value = dict(doc)
            if str(argv[-1]) == str(output):
                value["packets"] = [_packet(0, 1, "c" * 64)]
            return ProcessResult(0, json.dumps(value).encode(), b"", False, False, .01, False, False)
        return ProcessResult(0, b"", b"", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", mismatched)
    with pytest.raises(Mp3DecoderVerificationError, match="ordered source packets"):
        await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", (first,), output)

    async def decoder_failure(argv, **kwargs):
        if argv[0] == str(tmp_path / "ffprobe"):
            return ProcessResult(0, json.dumps(doc).encode(), b"", False, False, .01, False, False)
        return ProcessResult(1, b"", b"invalid frame", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", decoder_failure)
    with pytest.raises(Mp3DecoderVerificationError) as error:
        await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", (first,), output)
    assert error.value.code == "decoder_failed"


@pytest.mark.asyncio
async def test_probe_rejects_unmappable_decoded_frames(tmp_path: Path, monkeypatch) -> None:
    first, output = tmp_path / "a.mp3", tmp_path / "out.mp3"
    first.write_bytes(b"fixture")
    output.write_bytes(b"fixture")
    doc = _probe(packets=[_packet(0, 1, "a" * 64)], frames=[{"pkt_pos": "4", "nb_samples": 1_152}])

    async def fake_run_process(argv, **kwargs):
        if argv[0] == str(tmp_path / "ffprobe"):
            return ProcessResult(0, json.dumps(doc).encode(), b"", False, False, .01, False, False)
        return ProcessResult(0, b"", b"", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", fake_run_process)
    with pytest.raises(Mp3DecoderVerificationError, match="does not map"):
        await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", (first,), output)


@pytest.mark.asyncio
async def test_decoder_verification_rejects_changed_start_delay(tmp_path: Path, monkeypatch) -> None:
    first, output = tmp_path / "a.mp3", tmp_path / "out.mp3"
    first.write_bytes(b"fixture")
    output.write_bytes(b"fixture")
    source_doc = _probe(
        packets=[_packet(0, 1, "a" * 64, skip=20)],
        frames=[{"pkt_pos": "0", "nb_samples": 1_152}],
    )
    output_doc = _probe(
        packets=[_packet(0, 1, "a" * 64, skip=12)],
        frames=[{"pkt_pos": "0", "nb_samples": 1_152}],
    )

    async def fake_run_process(argv, **kwargs):
        if argv[0] == str(tmp_path / "ffprobe"):
            value = source_doc if str(argv[-1]) == str(first) else output_doc
            return ProcessResult(0, json.dumps(value).encode(), b"", False, False, .01, False, False)
        return ProcessResult(0, b"", b"", False, False, .01, False, False)

    monkeypatch.setattr("cognita.books.mp3_validation.run_process", fake_run_process)
    with pytest.raises(Mp3DecoderVerificationError) as error:
        await verify_chapter_mp3_decoder(tmp_path / "ffmpeg", tmp_path / "ffprobe", (first,), output)
    assert error.value.code == "delay_padding_mismatch"
