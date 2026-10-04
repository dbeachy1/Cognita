#!/usr/bin/env python3
"""Download and verify the two EasyOCR model files (DESIGN-LINUX-INSTALLER section 2.3, D5).

The images no longer carry the weights. This module is the one place that knows where they
come from and what their bytes must hash to. It is stdlib only and runs on the host (the
installers), in the release tooling and in tests.

The hashes are read from ``docs/easyocr-qualification-dependencies.json`` ``model_files``,
the one authority, and are never copied here. The two upstream zip URLs are the ones that
``release.py``'s ``stage_cpu_ocr_models`` used since 12.x (superseded in 14.2.0: that function is
gone; ``release.py`` now calls ``fetch`` into the target's model cache).

Why the download happens in a SIBLING directory: the OCR worker hashes EVERY file in the
model directory into its aggregate (``ocr_worker.py`` ``_verify_qualification``), so a
leftover partial download inside ``dest`` would disable OCR for good. ``fetch`` therefore
downloads into ``dest.parent / ".easyocr-download"`` and moves only a hash-verified file into
``dest`` with ``os.replace``. Afterwards ``dest`` holds exactly the two weight files.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
AUTHORITY = REPO / "docs" / "easyocr-qualification-dependencies.json"

# The upstream archives. (Copied from release.py's stage_cpu_ocr_models, deleted in 14.2.0; this is now
# the only place they are spelled.)
ORIGINS = {
    "craft_mlt_25k.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip",
    "english_g2.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip",
}

DOWNLOAD_DIR_NAME = ".easyocr-download"
READ_TIMEOUT_S = 60
PROGRESS_EVERY_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
CHUNK = 1024 * 1024


class OcrWeightsError(RuntimeError):
    """A weight file could not be obtained or did not match its published hash."""


def _load_weights() -> dict[str, dict[str, str]]:
    model_files = json.loads(AUTHORITY.read_text(encoding="utf-8"))["model_files"]
    return {name: {"url": url, "sha256": model_files[name]} for name, url in ORIGINS.items()}


# name -> {"url": upstream zip, "sha256": hash of the extracted .pth}
WEIGHTS: dict[str, dict[str, str]] = _load_weights()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _content_length(response: Any) -> int:
    """The Content-Length the server announced, or 0 when it did not (or a test opener has no headers)."""
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None and hasattr(headers, "get") else None
    return int(raw) if isinstance(raw, (str, int)) and str(raw).isdigit() else 0


def _download_archive(url: str, archive: Path, opener: Callable[..., Any], log: Any, name: str,
                      on_bytes: Callable[[int, int], None] | None = None) -> int:
    """Stream ``url`` to ``archive``, logging every 10 MB. Returns the byte count.

    ``on_bytes(count_so_far, announced_length)`` (design 19.1) is called every 10 MB and once at the end,
    with the announced Content-Length (0 when unknown); the caller turns it into the run-wide totals."""
    count = 0
    next_report = PROGRESS_EVERY_BYTES
    with opener(url, timeout=READ_TIMEOUT_S) as response, archive.open("wb") as output:
        announced = _content_length(response)
        while chunk := response.read(CHUNK):
            count += len(chunk)
            if count > MAX_ARCHIVE_BYTES:
                raise OcrWeightsError(f"{name}: archive exceeds {MAX_ARCHIVE_BYTES} bytes")
            output.write(chunk)
            if count >= next_report:
                log.line(f"ocr-weights: {name} downloaded {count // (1024 * 1024)} MB")
                if on_bytes is not None:
                    on_bytes(count, announced)
                next_report += PROGRESS_EVERY_BYTES
        if on_bytes is not None:
            on_bytes(count, announced)
    return count


def _extract_member(archive: Path, name: str, staged: Path) -> None:
    """Extract exactly the one member named ``name`` (any directory prefix) to ``staged``."""
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            path = Path(item.filename)
            mode = (item.external_attr >> 16) & 0o170000
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename or mode == stat.S_IFLNK:
                raise OcrWeightsError(f"{name}: unsafe member in archive: {item.filename!r}")
        candidates = [item for item in bundle.infolist() if Path(item.filename).name == name and not item.is_dir()]
        if len(candidates) != 1 or candidates[0].file_size > MAX_ARCHIVE_BYTES:
            raise OcrWeightsError(f"{name}: archive does not contain exactly one bounded {name}")
        with bundle.open(candidates[0]) as source, staged.open("wb") as output:
            while chunk := source.read(CHUNK):
                output.write(chunk)


def fetch(dest: Path, log: Any, *, opener: Callable[..., Any] | None = None,
          weights: dict[str, dict[str, str]] | None = None,
          progress: Callable[[int, int], None] | None = None) -> None:
    """Make ``dest`` hold exactly the verified weight files, downloading what is missing.

    ``log`` is any object with ``.line(str)`` (``release.Log`` fits). ``opener`` and
    ``weights`` exist for tests; the defaults are ``urllib.request.urlopen`` and ``WEIGHTS``.

    ``progress(done, total)`` (design 19.1) is optional and called about every 10 MB while an archive
    downloads: ``done`` is the bytes downloaded by this call so far across both files, ``total`` the
    sum of the Content-Length values of the archives started so far (0 when the server announced none).
    A callback that raises is logged and never fails the download.
    """
    opener = opener or urllib.request.urlopen
    weights = WEIGHTS if weights is None else weights
    dest = Path(dest)
    scratch = dest.parent / DOWNLOAD_DIR_NAME
    if dest.is_symlink() or scratch.is_symlink():
        raise OcrWeightsError(f"refusing a linked directory: {dest if dest.is_symlink() else scratch}")
    dest.mkdir(parents=True, exist_ok=True)
    log.line(f"ocr-weights: start dest={dest} files={len(weights)} scratch={scratch}")
    finished = 0          # bytes of archives already downloaded by this call
    announced_total = 0   # Content-Length of the archives finished so far

    def report_progress(name: str):
        def on_bytes(count: int, announced: int) -> None:
            if progress is None:
                return
            try:
                progress(finished + count, announced_total + announced)
            except Exception as exc:  # noqa: BLE001 - a progress report must never fail a download
                log.line(f"ocr-weights: the progress callback failed for {name}: {type(exc).__name__}: {exc}")
        return on_bytes

    if scratch.exists():
        # Left by an interrupted earlier run; it is ours and never inside dest.
        log.line(f"ocr-weights: removing leftover scratch directory {scratch}")
        shutil.rmtree(scratch)
    try:
        for name, spec in weights.items():
            target = dest / name
            expected = spec["sha256"]
            if target.is_file() and not target.is_symlink() and _sha256_file(target) == expected:
                log.line(f"ocr-weights: {name} already present and verified; skipped")
                continue
            if target.exists() or target.is_symlink():
                log.line(f"ocr-weights: {name} present but its hash is wrong; downloading it again")
            scratch.mkdir(parents=True, exist_ok=True)
            archive = scratch / (name + ".zip")
            staged = scratch / name
            try:
                log.line(f"ocr-weights: {name} downloading from {spec['url']}")
                size = _download_archive(spec["url"], archive, opener, log, name, report_progress(name))
                finished += size
                announced_total += size     # the finished archive's own length is exact
                log.line(f"ocr-weights: {name} archive complete bytes={size}")
                _extract_member(archive, name, staged)
                actual = _sha256_file(staged)
                if actual != expected:
                    raise OcrWeightsError(f"{name}: SHA-256 mismatch expected={expected} actual={actual}")
                os.replace(staged, target)
                target.chmod(0o644)
                log.line(f"ocr-weights: {name} verified and installed sha256={actual}")
            # http.client.HTTPException is not an OSError: a chunked body cut off mid-download raises
            # IncompleteRead, which must still end as OcrWeightsError, never a traceback.
            except (OSError, zipfile.BadZipFile, http.client.HTTPException) as exc:
                raise OcrWeightsError(f"{name}: download failed: {type(exc).__name__}: {exc}") from exc
            finally:
                # A failed or finished download leaves nothing behind in the scratch directory.
                archive.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)
        # dest is Cognita's own directory: anything but the weight files would enter the OCR
        # worker's aggregate hash and turn OCR off, so it goes.
        for entry in sorted(dest.iterdir()):
            if entry.name in weights:
                continue
            log.line(f"ocr-weights: removing stray entry {entry.name} from {dest}")
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    finally:
        try:
            scratch.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.line(f"ocr-weights: could not remove scratch directory {scratch}: {type(exc).__name__}: {exc}")
    log.line(f"ocr-weights: done dest={dest}")


class _PrintLog:
    def line(self, text: str) -> None:
        print(text, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and verify the EasyOCR model files.")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="download the weights into DIR")
    fetch_parser.add_argument("--dest", required=True, type=Path, help="the model directory (holds only the two .pth files)")
    args = parser.parse_args(argv)
    try:
        fetch(args.dest, _PrintLog())
    except OcrWeightsError as exc:
        print(f"ocr-weights: failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
