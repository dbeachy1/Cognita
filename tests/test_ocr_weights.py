"""scripts/ocr_weights.py: verified download of the EasyOCR weights (no network, no sleeps)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from scripts import ocr_weights


class ListLog:
    def __init__(self):
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


def make_zip(name: str, payload: bytes, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(prefix + name, payload)
    return buffer.getvalue()


class FakeOpener:
    """Stands in for urllib.request.urlopen: url -> bytes, records every call."""

    def __init__(self, archives: dict[str, bytes]):
        self.archives = archives
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        return io.BytesIO(self.archives[url])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def spec():
    good = {"a.pth": b"alpha bytes", "b.pth": b"bravo bytes"}
    weights = {name: {"url": f"https://example.invalid/{name}.zip", "sha256": sha(data)} for name, data in good.items()}
    archives = {weights[name]["url"]: make_zip(name, data, prefix="dir/") for name, data in good.items()}
    return good, weights, archives


def test_weights_hashes_come_from_the_authority_file():
    authority = json.loads(ocr_weights.AUTHORITY.read_text(encoding="utf-8"))["model_files"]
    assert set(ocr_weights.WEIGHTS) == {"craft_mlt_25k.pth", "english_g2.pth"}
    for name, entry in ocr_weights.WEIGHTS.items():
        assert entry["sha256"] == authority[name]
        assert entry["url"].startswith("https://github.com/JaidedAI/EasyOCR/releases/download/")


def test_good_download_lands_only_in_dest(tmp_path, spec):
    good, weights, archives = spec
    dest = tmp_path / "easyocr"
    opener = FakeOpener(archives)
    log = ListLog()
    ocr_weights.fetch(dest, log, opener=opener, weights=weights)
    assert {p.name for p in dest.iterdir()} == set(good)
    for name, data in good.items():
        assert (dest / name).read_bytes() == data
    assert all(timeout == 60 for _, timeout in opener.calls)
    assert not (tmp_path / ocr_weights.DOWNLOAD_DIR_NAME).exists()
    assert any("verified and installed" in line for line in log.lines)


def test_bad_hash_removes_the_download_and_leaves_dest_empty(tmp_path, spec):
    good, weights, archives = spec
    weights["a.pth"]["sha256"] = "0" * 64
    dest = tmp_path / "easyocr"
    with pytest.raises(ocr_weights.OcrWeightsError) as raised:
        ocr_weights.fetch(dest, ListLog(), opener=FakeOpener(archives), weights=weights)
    message = str(raised.value)
    assert "0" * 64 in message and sha(good["a.pth"]) in message
    assert list(dest.iterdir()) == []
    assert not (tmp_path / ocr_weights.DOWNLOAD_DIR_NAME).exists()


def test_existing_good_file_is_skipped(tmp_path, spec):
    good, weights, archives = spec
    dest = tmp_path / "easyocr"
    dest.mkdir()
    (dest / "a.pth").write_bytes(good["a.pth"])
    opener = FakeOpener(archives)
    log = ListLog()
    ocr_weights.fetch(dest, log, opener=opener, weights=weights)
    assert [url for url, _ in opener.calls] == [weights["b.pth"]["url"]]
    assert any("a.pth already present and verified; skipped" in line for line in log.lines)


def test_existing_wrong_file_is_replaced(tmp_path, spec):
    good, weights, archives = spec
    dest = tmp_path / "easyocr"
    dest.mkdir()
    (dest / "a.pth").write_bytes(b"truncated")
    ocr_weights.fetch(dest, ListLog(), opener=FakeOpener(archives), weights=weights)
    assert (dest / "a.pth").read_bytes() == good["a.pth"]


def test_stray_file_and_directory_in_dest_are_removed(tmp_path, spec):
    good, weights, archives = spec
    dest = tmp_path / "easyocr"
    (dest / "sub").mkdir(parents=True)
    (dest / "sub" / "inner.bin").write_bytes(b"x")
    (dest / "partial.zip").write_bytes(b"half")
    log = ListLog()
    ocr_weights.fetch(dest, log, opener=FakeOpener(archives), weights=weights)
    assert {p.name for p in dest.iterdir()} == set(good)
    assert any("removing stray entry partial.zip" in line for line in log.lines)
    assert any("removing stray entry sub" in line for line in log.lines)


def test_leftover_scratch_directory_from_an_interrupted_run_is_cleared(tmp_path, spec):
    good, weights, archives = spec
    scratch = tmp_path / ocr_weights.DOWNLOAD_DIR_NAME
    scratch.mkdir()
    (scratch / "a.pth.zip").write_bytes(b"old")
    dest = tmp_path / "easyocr"
    ocr_weights.fetch(dest, ListLog(), opener=FakeOpener(archives), weights=weights)
    assert not scratch.exists()
    assert {p.name for p in dest.iterdir()} == set(good)


def test_a_body_cut_off_mid_download_is_a_named_error_not_a_traceback(tmp_path, spec):
    # http.client.IncompleteRead is not an OSError; it must still become OcrWeightsError.
    import http.client

    _good, weights, archives = spec

    class CutOff(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"partial", 1000)

    def opener(url, timeout):
        return CutOff(archives[url])

    with pytest.raises(ocr_weights.OcrWeightsError, match="IncompleteRead"):
        ocr_weights.fetch(tmp_path / "easyocr", ListLog(), opener=opener, weights=weights)
    assert not (tmp_path / "easyocr").exists() or list((tmp_path / "easyocr").iterdir()) == []


def test_unsafe_archive_member_is_refused(tmp_path, spec):
    good, weights, archives = spec
    url = weights["a.pth"]["url"]
    archives[url] = make_zip("a.pth", good["a.pth"], prefix="../")
    with pytest.raises(ocr_weights.OcrWeightsError, match="unsafe member"):
        ocr_weights.fetch(tmp_path / "easyocr", ListLog(), opener=FakeOpener(archives), weights=weights)
    assert list((tmp_path / "easyocr").iterdir()) == []


def test_progress_is_logged_every_ten_megabytes(tmp_path):
    payload = b"\0" * (25 * 1024 * 1024)
    weights = {"big.pth": {"url": "https://example.invalid/big.zip", "sha256": sha(payload)}}
    # An all-zero payload deflates far below 10 MB, so store it instead to keep the archive large.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("big.pth", payload)
    archives = {"https://example.invalid/big.zip": buffer.getvalue()}
    log = ListLog()
    ocr_weights.fetch(tmp_path / "easyocr", log, opener=FakeOpener(archives), weights=weights)
    progress = [line for line in log.lines if "downloaded" in line and "MB" in line]
    assert [line.split("downloaded ")[1] for line in progress] == ["10 MB", "20 MB"]


def _stored_archive(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(name, payload)
    return buffer.getvalue()


def test_the_progress_callback_gets_cumulative_bytes_across_both_files(tmp_path, monkeypatch):
    # 19.1: about every 10 MB (shrunk here so the archives stay tiny) and once when each download ends.
    monkeypatch.setattr(ocr_weights, "PROGRESS_EVERY_BYTES", 300)
    monkeypatch.setattr(ocr_weights, "CHUNK", 100)
    first, second = b"a" * 1000, b"b" * 500
    weights = {"a.pth": {"url": "https://example.invalid/a.zip", "sha256": sha(first)},
               "b.pth": {"url": "https://example.invalid/b.zip", "sha256": sha(second)}}
    archives = {"https://example.invalid/a.zip": _stored_archive("a.pth", first),
                "https://example.invalid/b.zip": _stored_archive("b.pth", second)}
    seen: list[tuple[int, int]] = []
    ocr_weights.fetch(tmp_path / "easyocr", ListLog(), opener=FakeOpener(archives), weights=weights,
                      progress=lambda done, total: seen.append((done, total)))
    size_a, size_b = len(archives["https://example.invalid/a.zip"]), len(archives["https://example.invalid/b.zip"])
    assert seen, "the callback was never called"
    dones = [done for done, _total in seen]
    assert dones == sorted(dones)                                   # cumulative, never going back
    assert dones[-1] == size_a + size_b                             # ends at every byte of both archives
    assert any(size_a < done < size_a + size_b for done in dones)   # and it kept counting into the second file
    # This opener announces no length, so the total is only what the finished archives add up to.
    assert all(total == 0 for done, total in seen if done <= size_a)
    assert all(total == size_a for done, total in seen if done > size_a)


def test_the_progress_callback_sees_the_announced_length_when_the_server_sends_one(tmp_path):
    payload = b"x" * 400
    archive = _stored_archive("a.pth", payload)

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(archive))}

        def __enter__(self):
            return self

    weights = {"a.pth": {"url": "https://example.invalid/a.zip", "sha256": sha(payload)}}
    seen: list[tuple[int, int]] = []
    ocr_weights.fetch(tmp_path / "easyocr", ListLog(), opener=lambda url, timeout: Response(archive),
                      weights=weights, progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (len(archive), len(archive))


def test_a_raising_progress_callback_is_logged_and_the_download_finishes(tmp_path, spec):
    good, weights, archives = spec
    log = ListLog()

    def broken(done, total):
        raise RuntimeError("the progress file is full")

    ocr_weights.fetch(tmp_path / "easyocr", log, opener=FakeOpener(archives), weights=weights, progress=broken)
    assert (tmp_path / "easyocr" / "a.pth").read_bytes() == good["a.pth"]
    assert any("the progress callback failed" in line and "the progress file is full" in line for line in log.lines)


def test_cli_fetch_returns_1_on_failure(tmp_path, monkeypatch, capsys):
    def boom(dest, log, **_):
        raise ocr_weights.OcrWeightsError("nope")
    monkeypatch.setattr(ocr_weights, "fetch", boom)
    assert ocr_weights.main(["fetch", "--dest", str(tmp_path / "x")]) == 1
    assert "nope" in capsys.readouterr().err
