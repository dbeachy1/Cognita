"""Stage the exact Microsandbox wheel required by the runtime image.

The release build may fetch once into its task-owned build-artifacts directory;
the Dockerfile then installs only these bytes with hash checking and no index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

FILENAME = "microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl"
SHA256 = "848eab6e23b3bc168934b3093baafe26b086348d2914ae710900884e7bb74470"
INDEX_URL = "https://pypi.org/pypi/microsandbox/0.7.0/json"


def stage(source: Path | None, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / FILENAME
    if source is None:
        with urllib.request.urlopen(INDEX_URL, timeout=30) as response:
            metadata = json.load(response)
        urls = [item["url"] for item in metadata.get("urls", []) if item.get("filename") == FILENAME and item.get("digests", {}).get("sha256") == SHA256]
        if len(urls) != 1:
            raise SystemExit("PyPI did not expose exactly one locked Microsandbox wheel")
        with urllib.request.urlopen(urls[0], timeout=30) as response, target.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    else:
        shutil.copyfile(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != SHA256:
        target.unlink(missing_ok=True)
        raise SystemExit("staged Microsandbox wheel hash does not match the release lock")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path, default=Path("build-artifacts"))
    args = parser.parse_args()
    print(stage(args.source, args.destination))
