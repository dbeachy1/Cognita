"""14.2.0 (DESIGN-LINUX-INSTALLER 6.5, D5): the EasyOCR weights live in the model cache, not the image.

Nothing here downloads anything or starts a real worker: the process boundary is faked with in-memory
streams, and the model directories are tmp_path trees.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognita.assets import ocr_worker
from cognita.assets.ocr_worker import MISSING_WEIGHTS_MESSAGE, OCRWorkerError, OCRWorkerRunner

ROOT = Path(__file__).parents[1]
EXACT_REASON = "OCR model files are missing from the model cache. Rerun the installer to download them."


def _manifest(tmp_path: Path, names=("craft_mlt_25k.pth", "english_g2.pth")) -> Path:
    files = {name: hashlib.sha256(name.encode()).hexdigest() for name in names}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"engine": "easyocr==1.7.2", "runtime": {}, "model_files": {
        **files, "aggregate_sha256": hashlib.sha256("".join(files.values()).encode()).hexdigest()}}))
    return path


def test_the_missing_weights_reason_is_exactly_the_specified_sentence():
    assert MISSING_WEIGHTS_MESSAGE == EXACT_REASON


def test_the_service_points_the_ocr_worker_at_the_model_cache_and_keeps_the_manifest_in_the_image():
    source = (ROOT / "src/cognita/__main__.py").read_text(encoding="utf-8")
    assert 'config.ocr_model_dir = "/var/lib/cognita/models/easyocr"' in source
    assert '"/opt/cognita-models/easyocr-qualification.json"' in source
    assert "/opt/cognita-models/easyocr\"" not in source
    # The model-cache mount is what makes that path real: the installer, release.py and compose agree.
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "target: /var/lib/cognita/models\n" in compose
    assert 'dest = Path(env["COGNITA_MODEL_CACHE_ROOT"]) / "easyocr"' in (ROOT / "scripts/cognita_cli.py").read_text(encoding="utf-8")


def test_a_missing_model_directory_reports_the_missing_weights_reason_without_starting_a_worker(tmp_path, caplog):
    runner = OCRWorkerRunner(SimpleNamespace(ocr_python=str(Path(__file__)), ocr_model_dir=str(tmp_path / "absent")))
    with caplog.at_level("WARNING", logger="cognita.assets.ocr_worker"):
        with pytest.raises(OCRWorkerError) as raised:
            asyncio.run(runner.run(b"png", ("en",)))
    assert raised.value.reason == "ocr_unavailable"
    assert raised.value.message == EXACT_REASON
    # (The directory is logged with %r, which doubles Windows backslashes, so match its last component.)
    assert "absent" in caplog.text and EXACT_REASON in caplog.text


@pytest.mark.parametrize(("present", "wrong", "expected"), [
    ((), None, True),                                                 # the directory holds nothing
    (("craft_mlt_25k.pth",), None, True),                              # one of the two
    (("craft_mlt_25k.pth", "english_g2.pth"), None, False),            # both there: not missing
    (("craft_mlt_25k.pth", "english_g2.pth"), "english_g2.pth", False),  # wrong bytes are not "missing"
])
def test_weights_missing_distinguishes_absent_files_from_wrong_ones(tmp_path, present, wrong, expected):
    models = tmp_path / "easyocr"
    models.mkdir()
    for name in present:
        (models / name).write_bytes(b"wrong bytes" if name == wrong else name.encode())
    assert ocr_worker._weights_missing(str(models), str(_manifest(tmp_path))) is expected


def test_weights_missing_is_false_when_the_manifest_cannot_be_read(tmp_path):
    assert ocr_worker._weights_missing(str(tmp_path), str(tmp_path / "no-such-manifest.json")) is False
    (tmp_path / "bad.json").write_text("{")
    assert ocr_worker._weights_missing(str(tmp_path), str(tmp_path / "bad.json")) is False


def test_the_hash_check_still_refuses_a_wrong_file_and_a_missing_one(tmp_path, monkeypatch):
    """Unchanged since 5.x: moving the weights out of the image must not soften the verification."""
    import importlib.metadata
    manifest = _manifest(tmp_path)
    data = json.loads(manifest.read_text())
    data["runtime"] = {"torch": "9.9.9"}
    manifest.write_text(json.dumps(data))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    easyocr, torch = SimpleNamespace(__version__="1.7.2"), SimpleNamespace(__version__="9.9.9")
    models = tmp_path / "easyocr"
    models.mkdir()
    for name in ("craft_mlt_25k.pth", "english_g2.pth"):
        (models / name).write_bytes(name.encode())
    assert ocr_worker._verify_qualification(str(models), str(manifest), easyocr, torch)
    (models / "craft_mlt_25k.pth").write_bytes(b"tampered")
    assert not ocr_worker._verify_qualification(str(models), str(manifest), easyocr, torch)
    (models / "craft_mlt_25k.pth").unlink()
    assert not ocr_worker._verify_qualification(str(models), str(manifest), easyocr, torch)


class _Stdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Process:
    """Just enough of asyncio's Process for OCRWorkerRunner._run_framed: it answers with one error frame."""

    def __init__(self, reply: dict) -> None:
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(ocr_worker._frame(reply))
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = 0
        self.pid = 4242

    async def wait(self) -> int:
        return 0

    async def communicate(self):
        return b"", b""


async def _run_with_reply(tmp_path: Path, monkeypatch, reply: dict) -> OCRWorkerError:
    async def spawn(*_command, **_kwargs):
        return _Process(reply)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = OCRWorkerRunner(SimpleNamespace(ocr_python="python", ocr_model_dir=str(tmp_path), ocr_timeout_s=5.0))
    with pytest.raises(OCRWorkerError) as raised:
        await runner._run_framed({"image": ""}, timeout_seconds=5, device_id=None, cache_root=tmp_path)
    return raised.value


def test_the_workers_missing_weights_answer_reaches_the_caller_as_the_specified_sentence(tmp_path, monkeypatch):
    error = asyncio.run(_run_with_reply(tmp_path, monkeypatch, {
        "status": "error", "reason": "ocr_unavailable", "message": MISSING_WEIGHTS_MESSAGE}))
    assert (error.reason, error.message) == ("ocr_unavailable", EXACT_REASON)


def test_any_other_worker_diagnostic_is_still_not_forwarded(tmp_path, monkeypatch):
    error = asyncio.run(_run_with_reply(tmp_path, monkeypatch, {
        "status": "error", "reason": "ocr_unavailable", "message": "qualified OCR runtime/model tree is unavailable"}))
    assert (error.reason, error.message) == ("ocr_unavailable", "")
