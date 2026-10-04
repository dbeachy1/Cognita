#!/usr/bin/env bash
set -euo pipefail

# Provision the dedicated runtime; this never modifies Cognita's service venv.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="${1:-$ROOT/ocr-venv}"
MODEL_DIR="${2:-$ROOT/models/easyocr}"
MANIFEST="$ROOT/docs/easyocr-qualification-dependencies.json"
UV="${UV:-$HOME/.local/bin/uv}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/test/rocm7.1}"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.org/simple}"
TMP_BASE="${TMPDIR:-/tmp}"

[[ -x "$PYTHON" ]] || { echo "project Python is required at $PYTHON" >&2; exit 2; }
[[ -x "$(command -v curl || true)" ]] || { echo "curl is required for model provisioning" >&2; exit 2; }

# The root is task-owned before any download starts.  The trap covers success,
# assertion failure, and ordinary cancellation; no user directory is used as
# scratch space.
PROVISION_TMP="$(mktemp -d "$TMP_BASE/cognita-ocr-provision-XXXXXXXX")"
STAGED_TARGETS=()
cleanup() {
  for target in "${STAGED_TARGETS[@]}"; do
    rm -f -- "$target"
  done
  rm -rf -- "$PROVISION_TMP"
}
trap cleanup EXIT INT TERM

manifest_hash() {
  "$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["model_files"][sys.argv[2]])' \
    "$MANIFEST" "$1"
}

download_model() {
  local name="$1" url="$2" expected="$3"
  local archive="$PROVISION_TMP/$name.zip"
  local staged="$PROVISION_TMP/$name.pth"
  curl --fail --location --proto '=https' --tlsv1.2 --retry 2 --output "$archive" "$url"
  "$PYTHON" - "$archive" "$staged" "$name.pth" "$expected" <<'PY'
import hashlib
import os
import stat
import sys
import zipfile
from pathlib import PurePosixPath

archive, destination, expected_name, expected_hash = sys.argv[1:]
with zipfile.ZipFile(archive) as bundle:
    infos = bundle.infolist()
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe model archive member: {info.filename}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise SystemExit(f"symlink model archive member: {info.filename}")
    candidates = [info for info in infos if PurePosixPath(info.filename).name == expected_name and not info.is_dir()]
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one {expected_name} in model archive")
    info = candidates[0]
    digest = hashlib.sha256()
    temporary = destination + ".part"
    with bundle.open(info) as source, open(temporary, "wb") as output:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    actual = digest.hexdigest()
    if actual != expected_hash:
        os.unlink(temporary)
        raise SystemExit(f"{expected_name} SHA-256 mismatch: {actual}")
    os.replace(temporary, destination)
PY
}

[[ -x "$UV" ]] || { echo "uv is required at $UV" >&2; exit 2; }
"$UV" venv "$ENV_DIR" --python 3.13
"$UV" pip install --python "$ENV_DIR/bin/python" \
  --index-url "$PYTORCH_INDEX_URL" --extra-index-url "$PYPI_INDEX_URL" \
  --index-strategy unsafe-best-match \
  'easyocr==1.7.2' 'torch==2.10.0+rocm7.1' 'torchvision==0.25.0+rocm7.1' \
  'Pillow==11.3.0' 'opencv-python-headless==4.12.0.88' 'numpy==2.2.6' 'psutil==7.0.0'

STAGE_DIR="$PROVISION_TMP/models"
mkdir -p "$STAGE_DIR"
download_model craft_mlt_25k \
  "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip" \
  "$(manifest_hash craft_mlt_25k.pth)"
download_model english_g2 \
  "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip" \
  "$(manifest_hash english_g2.pth)"
mv "$PROVISION_TMP/craft_mlt_25k.pth" "$STAGE_DIR/craft_mlt_25k.pth"
mv "$PROVISION_TMP/english_g2.pth" "$STAGE_DIR/english_g2.pth"
mkdir -p "$(dirname "$MODEL_DIR")"
if [[ -e "$MODEL_DIR" ]]; then
  [[ -d "$MODEL_DIR" ]] || { echo "model path is not a directory: $MODEL_DIR" >&2; exit 2; }
  # Replace each verified file with an atomic rename while preserving any
  # unrelated upstream notices already present in the model directory.
  for model in craft_mlt_25k.pth english_g2.pth; do
    target="$MODEL_DIR/.$model.new.$$"
    STAGED_TARGETS+=("$target")
    install -m 0644 "$STAGE_DIR/$model" "$target"
    mv -f "$target" "$MODEL_DIR/$model"
  done
else
  mv "$STAGE_DIR" "$MODEL_DIR"
fi

"$PYTHON" -B "$ROOT/scripts/verify_ocr_runtime.py" \
  --python "$ENV_DIR/bin/python" --model-dir "$MODEL_DIR" --manifest "$MANIFEST"
