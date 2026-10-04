"""Focused UUID policy checks for the bounded AMD image preflight."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import scripts.amd_runtime_preflight as preflight
from cognita import gpu_worker


def test_embedding_preflight_uses_production_program_cache(monkeypatch, tmp_path) -> None:
    program_cache = tmp_path / "migraphx-cache"
    monkeypatch.setattr(preflight, "PROGRAM_CACHE", program_cache)
    monkeypatch.setenv("COGNITA_GPU_PROGRAM_CACHE_DIR", str(program_cache))
    assert preflight._program_cache_dir() == str(program_cache)
    assert program_cache.is_dir()

    monkeypatch.setenv("COGNITA_GPU_PROGRAM_CACHE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="^runtime_integrity_failed$"):
        preflight._program_cache_dir()


def test_uuidless_cards_are_reported_and_excluded_from_direct_canaries(capsys) -> None:
    cards = [
        SimpleNamespace(pci_address="0000:03:00.0", unique_id="0123abcd"),
        SimpleNamespace(pci_address="0000:7e:00.0", unique_id=None),
        SimpleNamespace(pci_address="0000:0b:00.0", unique_id="4567ef01"),
    ]

    bindable = preflight._uuid_bindable_cards(cards, "ocr")

    assert [card.pci_address for card in bindable] == [
        "0000:03:00.0",
        "0000:0b:00.0",
    ]
    assert "AMD_OCR_SKIPPED stable_uuid_missing=1" in capsys.readouterr().err


@pytest.mark.parametrize("component", ["embedding", "ocr"])
def test_uuidless_only_device_fails_qualification(component, capsys) -> None:
    cards = [SimpleNamespace(pci_address="0000:7e:00.0", unique_id=None)]

    with pytest.raises(RuntimeError, match="^stable_uuid_missing$"):
        preflight._uuid_bindable_cards(cards, component)
    assert f"AMD_{component.upper()}_SKIPPED stable_uuid_missing=1" in capsys.readouterr().err


def test_uuidless_only_failure_cannot_pass_preflight(monkeypatch, capsys) -> None:
    cards = [SimpleNamespace(pci_address="0000:7e:00.0", unique_id=None)]
    monkeypatch.setattr(sys, "argv", ["amd_runtime_preflight.py", "--component", "ocr"])
    monkeypatch.setattr(preflight, "_ocr", lambda _: preflight._uuid_bindable_cards(cards, "ocr"))

    assert preflight.main() == 1
    output = capsys.readouterr().err
    assert "AMD_OCR_SKIPPED stable_uuid_missing=1" in output
    assert "AMD_PREFLIGHT_FAILED stable_uuid_missing" in output


def test_embedding_session_must_read_back_the_selected_pci_card(monkeypatch) -> None:
    card = SimpleNamespace(pci_address="0000:03:00.0")
    monkeypatch.setattr(gpu_worker, "bound_pci_address", lambda: "0000:03:00.0")
    preflight._require_bound_pci(card)

    monkeypatch.setattr(gpu_worker, "bound_pci_address", lambda: "0000:0b:00.0")
    with pytest.raises(RuntimeError, match="^canary_failed$"):
        preflight._require_bound_pci(card)

    monkeypatch.setattr(gpu_worker, "bound_pci_address", lambda: None)
    with pytest.raises(RuntimeError, match="^canary_failed$"):
        preflight._require_bound_pci(card)


def test_multi_card_embedding_uses_isolated_child_per_card(monkeypatch) -> None:
    calls = []

    def successful_child(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preflight.subprocess, "run", successful_child)
    monkeypatch.setattr(preflight.time, "monotonic", lambda: 100.0)

    for uuid in ("0123abcd", "4567ef01"):
        preflight._run_isolated_embedding_card(
            SimpleNamespace(unique_id=uuid), 200.0,
        )

    assert [call[0][-1] for call in calls] == ["0123abcd", "4567ef01"]
    assert all(call[0][-4:-2] == ["--timeout", "100.0"] for call in calls)
    assert all(call[1]["stdout"] == preflight.subprocess.DEVNULL for call in calls)
    assert all(call[1]["stderr"] == preflight.subprocess.DEVNULL for call in calls)


@pytest.mark.parametrize("category", sorted(preflight._CATEGORIES))
def test_isolated_child_preserves_stable_failure_category(monkeypatch, category) -> None:
    monkeypatch.setattr(preflight.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        preflight.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=preflight._CHILD_FAILURE_CODES[category]),
    )

    with pytest.raises(RuntimeError, match=f"^{category}$"):
        preflight._run_isolated_embedding_card(SimpleNamespace(unique_id="0123abcd"), 200.0)


def test_isolated_child_timeout_is_categorized(monkeypatch) -> None:
    monkeypatch.setattr(preflight.time, "monotonic", lambda: 100.0)

    def timeout(*_args, **_kwargs):
        raise preflight.subprocess.TimeoutExpired("preflight", 100.0)

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    with pytest.raises(TimeoutError, match="^verification_timeout$"):
        preflight._run_isolated_embedding_card(SimpleNamespace(unique_id="0123abcd"), 200.0)


def test_isolated_child_returns_stable_category_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["amd_runtime_preflight.py", "--component", "embedding", "--card-uuid", "0123abcd"],
    )

    def failed_embedding(*_args):
        raise RuntimeError("provider_cpu_fallback")

    monkeypatch.setattr(preflight, "_embed", failed_embedding)

    assert preflight.main() == preflight._CHILD_FAILURE_CODES["provider_cpu_fallback"]
    assert capsys.readouterr().err == "AMD_PREFLIGHT_FAILED provider_cpu_fallback\n"
