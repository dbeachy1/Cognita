"""Verify and materialize the pinned wheel's runtime members during image build."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import importlib.metadata
import os
import shutil
from pathlib import Path

VERSION = "0.7.0"
ROOT = Path("/opt/microsandbox/0.7.0")


def _digest(value: bytes, algorithm: str) -> str:
    return base64.urlsafe_b64encode(hashlib.new(algorithm, value).digest()).rstrip(b"=").decode()


def _verify_record(record: Path, location: Path) -> None:
    """Verify every hashed PEP 376 RECORD row against its installed bytes."""
    with record.open("r", encoding="utf-8", newline="") as stream:
        for fields in csv.reader(stream):
            if len(fields) != 3 or not fields[0]:
                raise SystemExit("microsandbox RECORD entry is malformed")
            relative, hash_field, size_field = fields
            if not hash_field:
                if size_field:
                    raise SystemExit("microsandbox RECORD entry is malformed")
                continue
            algorithm, separator, encoded = hash_field.partition("=")
            if not separator or not algorithm or not encoded:
                raise SystemExit("microsandbox RECORD digest is malformed")
            try:
                expected_size = int(size_field) if size_field else None
            except ValueError as exc:
                raise SystemExit("microsandbox RECORD size is malformed") from exc
            if expected_size is not None and expected_size < 0:
                raise SystemExit("microsandbox RECORD size is malformed")
            path = location / relative
            if not path.is_file():
                raise SystemExit("microsandbox RECORD integrity verification failed")
            value = path.read_bytes()
            if expected_size is not None and len(value) != expected_size:
                raise SystemExit("microsandbox RECORD integrity verification failed")
            try:
                actual = _digest(value, algorithm)
            except ValueError as exc:
                raise SystemExit("microsandbox RECORD digest algorithm is unsupported") from exc
            if not hmac.compare_digest(actual, encoded):
                raise SystemExit("microsandbox RECORD integrity verification failed")


def verify(*, materialize: bool = False, runtime_root: Path = ROOT) -> list[Path]:
    distribution = importlib.metadata.distribution("microsandbox")
    if distribution.version != VERSION:
        raise SystemExit("microsandbox distribution version is not pinned")
    location = Path(distribution.locate_file(""))
    runtime_members: list[Path] = []
    record = next((Path(distribution.locate_file(item)) for item in distribution.files or () if str(item).endswith(".dist-info/RECORD")), None)
    if record and record.is_file():
        _verify_record(record, location)
    for item in distribution.files or ():
        path = Path(item)
        if path.name == "msb" or path.name.startswith("libkrunfw"):
            source = location / path
            if source.is_file():
                runtime_members.append(source)
    if not runtime_members:
        raise SystemExit("wheel-bundled msb/libkrunfw runtime members were not found")
    if materialize:
        runtime_root.mkdir(parents=True, exist_ok=True)
        for source in runtime_members:
            target = runtime_root / source.name
            shutil.copy2(source, target)
            os.chmod(target, 0o755 if target.name == "msb" else 0o644)
    return runtime_members


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    args = parser.parse_args()
    verify(materialize=args.materialize, runtime_root=args.runtime_root)
