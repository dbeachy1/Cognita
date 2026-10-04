#!/usr/bin/env python3
"""Cognita-owned EasyOCR/PyTorch qualification gate.

The process is deliberately one device per invocation.  GPU invocations must
be launched with ROCR_VISIBLE_DEVICES=GPU-<UUID>; this prevents a successful
run from silently selecting a different card.  Model downloads are forbidden
by both EasyOCR and an offline socket guard.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any, Self


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _bbox(poly: Any, width: int, height: int) -> list[int]:
    points = [(float(p[0]), float(p[1])) for p in poly]
    return [max(0, min(width, round(min(x for x, _ in points)))),
            max(0, min(height, round(min(y for _, y in points)))),
            max(0, min(width, round(max(x for x, _ in points)))),
            max(0, min(height, round(max(y for _, y in points))))]


def _iou(a: list[int], b: list[int]) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return inter / (area_a + area_b - inter) if area_a + area_b else 0.0


def _reading_order(regions: list[dict[str, Any]], width: int) -> list[dict[str, Any]]:
    """Apply the documented line order, with a persistent gutter as a column break."""
    if len(regions) < 2:
        return regions
    by_x = sorted(regions, key=lambda item: (item["bbox"][0], item["bbox"][1]))
    gaps = [by_x[i + 1]["bbox"][0] - by_x[i]["bbox"][2] for i in range(len(by_x) - 1)]
    largest = max(gaps, default=0)
    if largest > max(100, width * 0.15):
        split = gaps.index(largest) + 1
        columns = (by_x[:split], by_x[split:])
        return [item for column in columns for item in sorted(column, key=lambda x: (x["bbox"][1], x["bbox"][0]))]
    return sorted(regions, key=lambda item: (item["bbox"][1], item["bbox"][0], item["bbox"][3], item["bbox"][2], item["text"]))


class OfflineSocketGuard:
    """Reject all outbound connects and retain a count for the evidence."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._original = socket.socket.connect

    def __enter__(self) -> Self:
        def blocked(sock: socket.socket, address: Any) -> None:
            self.attempts.append(str(address).split(" ", 1)[0])
            raise OSError("Cognita OCR qualification is offline")
        socket.socket.connect = blocked  # type: ignore[method-assign]
        return self

    def __exit__(self, *_: object) -> None:
        socket.socket.connect = self._original  # type: ignore[method-assign]


def _model_manifest(model_dir: Path) -> dict[str, Any]:
    files = []
    for path in sorted(model_dir.rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(model_dir)), "bytes": path.stat().st_size,
                          "sha256": _sha256(path)})
    digest = hashlib.sha256("".join(item["sha256"] for item in files).encode()).hexdigest()
    return {"directory": str(model_dir), "files": files, "sha256": digest}


def _rocminfo() -> str:
    proc = subprocess.run(["rocminfo"], capture_output=True, text=True, check=False, timeout=20)
    return proc.stdout + proc.stderr


def _device_proof(device: str, torch: Any) -> dict[str, Any]:
    if device == "cpu":
        if torch.cuda.is_available():
            # CPU qualification must remain explicit and must not accidentally
            # report an available GPU as the selected computation device.
            selected = "cpu"
        else:
            selected = "cpu"
        return {"selected": selected, "torch_device": "cpu", "cuda_available": bool(torch.cuda.is_available())}
    visible = os.environ.get("ROCR_VISIBLE_DEVICES", "")
    match = re.fullmatch(r"GPU-[0-9a-fA-F]+", visible.strip())
    if not match:
        raise RuntimeError("GPU run requires exactly one ROCR_VISIBLE_DEVICES=GPU-<UUID>")
    info = _rocminfo()
    if info.count(visible) != 1:
        raise RuntimeError(f"rocminfo did not expose exactly one bound UUID: {visible}")
    if not re.search(r"Marketing Name:\s+AMD Radeon AI PRO R9700", info) or not re.search(r"Name:\s+gfx1201", info):
        raise RuntimeError("bound HIP agent is not an AMD Radeon AI PRO R9700/gfx1201")
    if not torch.version.hip or not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("PyTorch ROCm did not expose exactly one usable CUDA/HIP device")
    torch.cuda.set_device(0)
    props = torch.cuda.get_device_properties(0)
    # A real tensor operation plus a device-resident model check prevents a
    # CPU fallback from being mistaken for a GPU qualification.
    probe = torch.ones((256, 256), device="cuda") @ torch.ones((256, 256), device="cuda")
    torch.cuda.synchronize()
    if not probe.is_cuda or float(probe[0, 0].item()) != 256.0:
        raise RuntimeError("HIP tensor probe did not execute on the selected GPU")
    del probe
    return {"selected": visible, "torch_device": "cuda:0", "torch_name": props.name,
            "torch_index": 0, "torch_device_count": torch.cuda.device_count(),
            "torch_hip": torch.version.hip, "rocminfo_uuid": visible,
            "rocminfo_sha256": hashlib.sha256(info.encode()).hexdigest(),
            "total_memory_bytes": int(props.total_memory)}


def _read_result(reader: Any, image_path: Path, device: str) -> tuple[dict[str, Any], float, dict[str, Any]]:
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as image:
        image.load()
        width, height = image.size
        array = np.array(image.convert("RGB"))
    if device != "cpu":
        _reader_device = reader.device  # force selected-device attribute access in evidence
    if device != "cpu":
        reader.detector.eval()
        reader.recognizer.eval()
    start = time.perf_counter()
    raw = reader.readtext(array, detail=1, paragraph=False, decoder="greedy", batch_size=1,
                          workers=0, canvas_size=2048, mag_ratio=1.0,
                          text_threshold=0.4, low_text=0.3, link_threshold=0.3,
                          slope_ths=0.1, ycenter_ths=0.5, height_ths=0.5, width_ths=0.5,
                          add_margin=0.1)
    if device != "cpu":
        import torch
        torch.cuda.synchronize()
    duration = (time.perf_counter() - start) * 1000
    regions = []
    for poly, text, confidence in raw:
        regions.append({"text": _normalize(str(text)), "polygon": [[round(p[0]), round(p[1])] for p in poly],
                        "bbox": _bbox(poly, width, height), "confidence": float(confidence)})
    regions = _reading_order(regions, width)
    normalized = "\n".join(item["text"] for item in regions)
    return {"width": width, "height": height, "text": _normalize(normalized), "regions": regions,
            "outcome": "no_text" if not regions else "text"}, duration, {"raw_count": len(raw)}


def _cer(actual: str, expected: str) -> float:
    matcher = difflib.SequenceMatcher(a=expected, b=actual)
    return 1.0 - matcher.ratio() if expected else (0.0 if not actual else 1.0)


def _fixture_evaluation(name: str, spec: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected = "\n".join(spec["expected_lines"])
    if spec.get("exact") and result["text"] != expected:
        raise RuntimeError(f"{name}: exact normalized text mismatch")
    if spec.get("purpose") == "CER <= 2 percent" and result["cer"] > 0.02:
        raise RuntimeError(f"{name}: CER {result['cer']:.4f} exceeds 0.02")
    if name == "blank.png" and result["outcome"] != "no_text":
        raise RuntimeError("blank.png: expected no_text")
    if spec.get("purpose") == "column reading order":
        boxes = [region["bbox"] for region in result["regions"]]
        if len(boxes) != len(spec["expected_lines"]):
            raise RuntimeError(f"{name}: expected one detected region per annotated line")
        # The first complete column must precede the second complete column;
        # exact character recognition remains covered by the canonical fixture.
        width = result["width"]
        if not all(boxes[i][0] < width * 0.5 for i in range(3)) or not all(boxes[i][0] >= width * 0.5 for i in range(3, 6)):
            raise RuntimeError(f"{name}: column reading order/geometry mismatch")
    warnings: list[str] = []
    if name == "degraded.png":
        if result["cer"] <= 0.0 or not result["regions"]:
            raise RuntimeError("degraded.png: expected partial detected text, not fabricated empty success")
        if result["cer"] > 0.02:
            warnings.append("partial_text")
        if any(region["confidence"] < 0.60 for region in result["regions"]):
            warnings.append("low_confidence")
        if not warnings:
            raise RuntimeError("degraded.png: engine returned a confident result without uncertainty")
    return {"warnings": warnings}


def _compare_reference(reference: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for name, result in results.items():
        ref = reference["fixtures"][name]
        pairs = list(zip(ref["regions"], result["regions"], strict=False))
        if len(pairs) != len(ref["regions"]) or len(pairs) != len(result["regions"]):
            raise RuntimeError(f"{name}: CPU/GPU region count mismatch")
        ious = [_iou(a["bbox"], b["bbox"]) for a, b in pairs]
        confidence_diffs = [abs(a["confidence"] - b["confidence"]) for a, b in pairs]
        if ious and min(ious) < 0.90:
            raise RuntimeError(f"{name}: CPU/GPU box IoU below 0.90: {min(ious):.3f}")
        if confidence_diffs and max(confidence_diffs) > 0.10:
            raise RuntimeError(f"{name}: CPU/GPU confidence difference above 0.10: {max(confidence_diffs):.3f}")
        comparisons[name] = {"regions": len(pairs), "min_box_iou": min(ious, default=1.0),
                             "max_confidence_difference": max(confidence_diffs, default=0.0)}
    return comparisons


def qualify(args: argparse.Namespace) -> dict[str, Any]:
    # TemporaryDirectory registers cleanup at creation; no child process owns
    # it.  The finally block below additionally verifies its absence.
    temp = tempfile.TemporaryDirectory(prefix="cognita-easyocr-qual-")
    temp_root = Path(temp.name).resolve()
    try:
        fixture_dir = args.fixture_dir.resolve()
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        model_dir = args.model_dir.resolve()
        if not model_dir.is_dir():
            raise RuntimeError(f"model directory is absent: {model_dir}")
        with OfflineSocketGuard() as network_guard:
            import cv2
            import easyocr
            import PIL
            import torch

            proof = _device_proof(args.device, torch)
            gpu_arg: Any = False if args.device == "cpu" else "cuda:0"
            cold_start = time.perf_counter()
            reader = easyocr.Reader(["en"], gpu=gpu_arg, model_storage_directory=str(model_dir),
                                     download_enabled=False, verbose=False, detector=True, recognizer=True)
            if args.device != "cpu":
                model_devices = {str(param.device) for module in (reader.detector, reader.recognizer)
                                 for param in module.parameters()}
                if model_devices != {"cuda:0"}:
                    raise RuntimeError(f"EasyOCR models are not bound to cuda:0: {sorted(model_devices)}")
            model_load_ms = (time.perf_counter() - cold_start) * 1000

            if args.device != "cpu":
                torch.cuda.reset_peak_memory_stats()
                free_before, total_vram = torch.cuda.mem_get_info()
            results: dict[str, Any] = {}
            for fixture_name, spec in manifest["fixtures"].items():
                image_path = fixture_dir / fixture_name
                first, cold_ms, extra = _read_result(reader, image_path, args.device)
                warm = []
                for _ in range(2):
                    _warm_result, duration, _ = _read_result(reader, image_path, args.device)
                    warm.append(duration)
                first["cold_ms"] = cold_ms
                first["warm_ms"] = warm
                first["warm_median_ms"] = median(warm)
                first["expected_lines"] = spec["expected_lines"]
                first["cer"] = _cer(first["text"], "\n".join(spec["expected_lines"]))
                first["raw"] = extra
                first.update(_fixture_evaluation(fixture_name, spec, first))
                results[fixture_name] = first
            if args.device != "cpu":
                torch.cuda.synchronize()
                free_after, _ = torch.cuda.mem_get_info()
                peak_allocated = int(torch.cuda.max_memory_allocated())
                peak_reserved = int(torch.cuda.max_memory_reserved())
                memory = {"free_before_bytes": int(free_before), "free_after_bytes": int(free_after),
                          "total_bytes": int(total_vram), "peak_allocated_bytes": peak_allocated,
                          "peak_reserved_bytes": peak_reserved,
                          "headroom_after_bytes": int(free_after)}
            else:
                memory = {"process_memory": "not sampled on CPU qualification"}
            package_versions = {"easyocr": easyocr.__version__, "torch": torch.__version__,
                                "torchvision": __import__("torchvision").__version__,
                                "Pillow": PIL.__version__, "opencv-python": cv2.__version__,
                                "numpy": __import__("numpy").__version__}
            model_manifest = _model_manifest(model_dir)
            if network_guard.attempts:
                raise RuntimeError(f"offline runtime attempted network connections: {network_guard.attempts}")
            comparisons = None
            if args.reference:
                comparisons = _compare_reference(json.loads(args.reference.read_text(encoding="utf-8")), results)
            # Clean the registered private root before writing evidence so the
            # artifact records the actual post-cleanup state.
            temp.cleanup()
            cleanup_verified = not temp_root.exists()
            if not cleanup_verified:
                raise RuntimeError(f"owned temporary root was not removed: {temp_root}")
            report: dict[str, Any] = {
                "qualification_version": 1, "status": "PASS", "device": args.device,
                "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "host": platform.node(), "platform": platform.platform(),
                "offline": {"download_enabled": False, "socket_connect_attempts": network_guard.attempts,
                            "network_result": "PASS: no outbound connect attempted"},
                "device_proof": proof, "package_versions": package_versions,
                "model_manifest": model_manifest, "model_load_ms": model_load_ms,
                "memory": memory, "fixtures": results,
                "cpu_reference_comparison": comparisons,
                "temp_cleanup": {"root": str(temp_root), "verified_absent": cleanup_verified},
            }
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return report
    finally:
        temp.cleanup()
        # TemporaryDirectory's cleanup is part of the gate evidence.  Do not
        # claim cleanup if a platform sharing violation leaves the root behind.
        if temp_root.exists():
            raise RuntimeError(f"owned temporary root was not removed: {temp_root}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "gpu"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    try:
        report = qualify(args)
    except Exception as exc:  # noqa: BLE001 - CLI must preserve a safe failure summary
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}, indent=2))
        return 1
    # Do not print OCR text to ordinary logs.  The JSON artifact contains it
    # for deterministic acceptance comparison and is explicitly scoped output.
    print(json.dumps({"status": report["status"], "device": report["device"],
                      "model_load_ms": report["model_load_ms"],
                      "fixture_count": len(report["fixtures"]),
                      "offline": report["offline"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
