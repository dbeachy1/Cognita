# EasyOCR/PyTorch qualification gate (Cognita 10.0)

Status: **PASS** on CPU and two AMD GPUs.

This qualification checks OCR accuracy and runtime behavior using synthetic
fixtures. It does not implement the OCR service, cache, scheduler integration,
or asset search. The harness runs one process per device, uses
`download_enabled=False`, and blocks `socket.connect`; the fixtures contain no
user data.

## Reproduction

From the repository root, generate fixtures with:

```text
python -B scripts/generate_ocr_fixtures.py
```

Run the commands from a clean task-owned Python environment. Replace
`<gpu-id>` with a device identifier reported by the local ROCm runtime:

```text
ROCR_VISIBLE_DEVICES=<gpu-id> env -u HIP_VISIBLE_DEVICES <venv>/bin/python -B <repo>/scripts/qualify_easyocr.py --fixture-dir <repo>/tests/fixtures/ocr_qualification --model-dir <root>/models --device gpu --reference <root>/cpu.json --output <root>/gpu.json
<venv>/bin/python -B <repo>/scripts/qualify_easyocr.py --fixture-dir <repo>/tests/fixtures/ocr_qualification --model-dir <root>/models --device cpu --output <root>/cpu.json
```

The harness verifies that each selected GPU is visible to PyTorch and that
EasyOCR detector and recognizer tensors execute on the selected device. Confirm
the device identity with `rocminfo` before comparing CPU and GPU results.

## Dependencies and model evidence

Exact pins, installed-tree fingerprints, model hashes, and package/model
license notes are in [easyocr-qualification-dependencies.json](easyocr-qualification-dependencies.json).
The model aggregate SHA-256 is
`3f083948a0cd26d77c8be07f8fa38ee5a63f76cfab5bbe720b4d1d9bb5df3790`.
The fixture generator uses the bundled `tests/fixtures/ocr_qualification/DejaVuSans.ttf`
(SHA-256 `b4c632e3cdf9acc7f28758fb5a323c8524d7fc6660d46904d9b6cbe2809c419c`).
It is DejaVu Sans, a Bitstream Vera derivative with DejaVu changes in the public
domain; retain the Bitstream Vera permission notice when distributing the font
or fixtures.

## Acceptance evidence

All seven fixtures passed on every device: canonical multiline text is exact,
the small-font fixture has CER 0, multi-column reading order is exact, dark and
transparent examples are exact, and blank returns `no_text`. The intentionally
degraded fixture produced partial text plus `partial_text` and `low_confidence`
warnings (not a confident fabricated empty result).

GPU results matched the CPU reference with minimum box IoU **1.000** for every
fixture and maximum confidence difference **0.053** (below 0.10). Cold model
load was 0.72–0.74 s. GPU cold fixture times ranged 0.19–1.47 s after runtime
initialization and warm times were approximately 0.11–0.40 s. CPU warm fixture
times were approximately 0.04–0.63 s.

Every run recorded zero outbound socket attempts and
`download_enabled=false`. Remove task-owned temporary result and model files
after qualification, once each process has exited.

The qualification artifacts were retained only long enough to write this
report and are not committed because they contain repeated OCR transcripts;
the deterministic fixtures, generator, report, and dependency evidence are
the durable gate record.
