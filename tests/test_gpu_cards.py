"""`gpu_cards` — the 6.4 card-selection setting.

This is the only GPU key a normal user is expected to hand-edit, which changes
what the tests owe it. The interesting cases are not "does index 1 resolve to
the second card" but the ways a hand-edited value goes wrong: a word where a
list was expected, an index for a card that is not there, and `false` written
by someone who meant "off". Every one of those degrades to the CPU by design,
so a mistake here is invisible unless something says so out loud — and that
"something" is what most of these tests pin.
"""

from __future__ import annotations

import pytest

from cognita.config import CognitaConfig
from cognita.gpu_probe import (
    GpuDevice,
    SysfsAmdProbe,
    gate_devices,
    normalize_cards,
    qualifying,
    resolve_cards,
    skipped_summary,
)
from tests.test_gpu_probe import make_card

GIB = 1024 ** 3


def dev(sysfs: str, pci: str, *, free_gb: float = 30.0) -> GpuDevice:
    return GpuDevice(
        sysfs_name=sysfs,
        pci_address=pci,
        unique_id=None,
        name=sysfs,
        vram_total=int(32 * GIB),
        vram_free=int(free_gb * GIB),
        busy_percent=0,
    )


REFERENCE = [
    dev("card1", "0000:03:00.0"),
    dev("card2", "0000:07:00.0"),
    dev("card3", "0000:7e:00.0"),
]


# --------------------------------------------------------------------------
# Parsing what a human typed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("all", "all"),
    ("ALL", "all"),
    (" all ", "all"),
    ("*", "all"),
    ("", "all"),
    (None, "all"),
    ("none", []),
    ("off", []),
    ("cpu", []),
    ([], []),
    (0, [0]),
    (2, [2]),
    ([1, 0], [0, 1]),
    ([2, 0, 2], [0, 2]),
])
def test_accepted_forms(value, expected):
    assert normalize_cards(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("0", [0]),
    ("1", [1]),
    ("0,2", [0, 2]),
    ("[0, 2]", [0, 2]),
    ("[1]", [1]),
    ("[]", []),
])
def test_numbers_written_as_text_because_the_environment_has_no_other_kind(
    value, expected,
):
    """🔴 `COGNITA_GPU_CARDS=0` must work — it is a documented override.

    `_apply_env_overrides` assigns the raw string for every key. Other fields
    survive that because pydantic coerces "8675" for a field typed `int`; this
    one is typed `Any` so that a bare `false` cannot be coerced to card 0, and
    the price of that choice is that nothing coerces the legitimate forms
    either. Parsing them here is what pays it.
    """
    assert normalize_cards(value) == expected


def test_env_override_round_trips_through_the_real_loader(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNITA_GPU_CARDS", "[0, 1]")
    from cognita.config import load_config

    config = load_config(tmp_path / "does-not-exist.yaml")
    assert normalize_cards(config.gpu_cards) == [0, 1]


@pytest.mark.parametrize("value", [True, False])
def test_a_bare_boolean_is_refused_rather_than_read_as_card_zero(value):
    """🔴 The trap this parser exists for.

    `gpu_cards: false` is what someone writes when they mean "no GPU". Python's
    `bool` is an `int`, so the obvious implementation reads it as index 0 and
    ENABLES the first card — the exact opposite, silently, on the machine of
    someone who was trying to turn the feature off. The bool check must come
    before the int branch, and this is the test that keeps it there.
    """
    with pytest.raises(ValueError) as err:
        normalize_cards(value)
    assert "'all'" in str(err.value) and "'none'" in str(err.value)


@pytest.mark.parametrize("value,fragment", [
    ("gpu0", "unrecognized value"),
    ("first", "unrecognized value"),
    (-1, "start at 0"),
    ([1.5], "whole numbers"),
    (["0"], "whole numbers"),
    ({"card": 0}, "expected"),
])
def test_rejections_say_what_to_write_instead(value, fragment):
    with pytest.raises(ValueError) as err:
        normalize_cards(value)
    assert fragment in str(err.value)


def test_the_error_is_raised_at_startup_not_mid_walk():
    """Config validation, so a typo fails the service rather than the index.

    Every consumer of this setting sits behind the CPU fallback, where a bad
    value degrades instead of failing. Startup is the last place a mistake is
    still loud.
    """
    with pytest.raises(ValueError):
        CognitaConfig(gpu_cards="gpu0")
    with pytest.raises(ValueError):
        CognitaConfig(gpu_cards=False)
    assert CognitaConfig().gpu_cards == "all"


# --------------------------------------------------------------------------
# Resolving indices against the cards that are actually present
# --------------------------------------------------------------------------


def test_all_means_no_restriction_not_a_list_of_everything():
    """`None` and "every address" differ when a card appears mid-life.

    A pinned list is a snapshot; `None` is a standing instruction. Resolving
    "all" into today's addresses would silently exclude a card added later.
    """
    assert resolve_cards(REFERENCE, "all").pinned is None


def test_none_selects_nothing_and_the_gate_agrees():
    selection = resolve_cards(REFERENCE, "none")
    assert selection.pinned == []
    assert selection.selects_nothing
    results = gate_devices(
        REFERENCE, batch_ceiling_gb=1.0, reserve_vram_gb=1.0,
        max_busy_percent=90, pinned=selection.pinned,
        pinned_by=selection.source,
    )
    assert qualifying(results) == []
    assert all("gpu_cards=none" in line for line in skipped_summary(results))


def test_an_index_resolves_to_a_pci_address():
    """The index never leaves this function — everything below is an address."""
    assert resolve_cards(REFERENCE, 1).pinned == ["0000:07:00.0"]
    assert resolve_cards(REFERENCE, [0, 2]).pinned == [
        "0000:03:00.0", "0000:7e:00.0",
    ]


def test_out_of_range_index_warns_and_keeps_the_valid_ones():
    selection = resolve_cards(REFERENCE, [0, 5])
    assert selection.pinned == ["0000:03:00.0"]
    assert len(selection.warnings) == 1
    assert "card 5" in selection.warnings[0] and "0-2" in selection.warnings[0]


def test_selecting_only_a_missing_card_warns_that_it_will_use_the_cpu():
    """🔴 The silent-slowdown case.

    `[5]` on a three-card box selects nothing, falls back to the CPU, and looks
    exactly like a machine with no GPU. Two warnings: the bad index, and the
    consequence — because the consequence is the part the user cares about and
    the part no other subsystem will ever mention.
    """
    selection = resolve_cards(REFERENCE, [5])
    assert selection.pinned == []
    assert any("card 5" in w for w in selection.warnings)
    assert any("use the CPU" in w for w in selection.warnings)


def test_no_cards_present_does_not_warn_about_selecting_nothing():
    """A machine with no GPU is the ordinary case, not a misconfiguration."""
    selection = resolve_cards([], [0])
    assert selection.pinned == []
    assert not any("use the CPU" in w for w in selection.warnings)


def test_pci_addresses_override_indices():
    """The more specific statement wins, and the log says which one applied."""
    selection = resolve_cards(REFERENCE, [0], ["0000:7e:00.0"])
    assert selection.pinned == ["0000:7e:00.0"]
    assert selection.source == "gpu_device_ids"
    assert selection.warnings == []


def test_gate_names_the_setting_that_excluded_the_card():
    """`devices_skipped` used to say "not in gpu_device_ids" unconditionally."""
    selection = resolve_cards(REFERENCE, [0])
    results = gate_devices(
        REFERENCE, batch_ceiling_gb=1.0, reserve_vram_gb=1.0,
        max_busy_percent=90, pinned=selection.pinned,
        pinned_by=selection.source,
    )
    skipped = skipped_summary(results)
    assert [d.sysfs_name for d in qualifying(results)] == ["card1"]
    assert any("gpu_cards=[0]" in line for line in skipped)
    assert not any("gpu_device_ids" in line for line in skipped)


def test_skipped_summary_carries_the_index_to_type():
    """The rejected card is the one someone is about to go and configure."""
    results = gate_devices(
        REFERENCE, batch_ceiling_gb=1.0, reserve_vram_gb=1.0,
        max_busy_percent=90, pinned=["0000:03:00.0"],
        pinned_by="gpu_cards=[0]",
    )
    assert skipped_summary(results) == [
        "card2[1]:not in gpu_cards=[0]",
        "card3[2]:not in gpu_cards=[0]",
    ]


# --------------------------------------------------------------------------
# The startup banner — where a user finds out what 0 and 1 mean
# --------------------------------------------------------------------------


class FakeProbe:
    def __init__(self, devices):
        self._devices = devices

    def devices(self):
        return self._devices


def banner(caplog, config, devices=REFERENCE):
    import logging

    from cognita.gpu_probe import log_available_cards

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        log_available_cards(config, probe=FakeProbe(devices))
    return [r.getMessage() for r in caplog.records]


def gpu_config(**kw):
    return CognitaConfig(gpu_enabled=True, gpu_venv_python="/x/python", **kw)


def test_the_banner_prints_the_index_for_every_card(caplog):
    """The whole point: `0` and `1` are meaningless until something prints them."""
    lines = banner(caplog, gpu_config())
    assert any("card 0" in ln and "card1" in ln and "0000:03:00.0" in ln
               for ln in lines)
    assert any("card 1" in ln and "card2" in ln for ln in lines)
    assert any("card 2" in ln and "card3" in ln for ln in lines)
    assert any("gpu_cards" in ln and "config/cognita.yaml" in ln for ln in lines)


def test_the_banner_marks_which_cards_the_setting_selected(caplog):
    lines = banner(caplog, gpu_config(gpu_cards=[0]))
    card1 = next(ln for ln in lines if "card 0" in ln)
    card2 = next(ln for ln in lines if "card 1" in ln)
    assert "usable" in card1 and "unusable" not in card1
    assert "unusable: not in gpu_cards=[0]" in card2


def test_an_unusable_card_says_why(caplog):
    """A card excluded for VRAM must not read the same as one excluded by config."""
    devices = [dev("card1", "0000:03:00.0"), dev("card3", "0000:7e:00.0", free_gb=1.8)]
    lines = banner(caplog, gpu_config(), devices)
    assert any("card 1" in ln and "unusable: vram_free=" in ln for ln in lines)


def test_a_machine_with_no_cards_says_so_in_one_line(caplog):
    """The ordinary case on Windows and on any box without a GPU. Not an error."""
    lines = banner(caplog, gpu_config(), [])
    assert lines == ["GPU cards   : none found — indexing will use the CPU"]


def test_cards_are_still_listed_when_acceleration_is_off(caplog):
    """Someone about to enable the GPU needs the indices before they enable it."""
    lines = banner(caplog, CognitaConfig())
    assert any("acceleration OFF" in ln for ln in lines)
    assert any("card 0" in ln for ln in lines)


def test_a_bad_index_is_warned_about_in_the_banner(caplog):
    """Earliest possible catch — every later symptom is silence plus slowness."""
    import logging

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        from cognita.gpu_probe import log_available_cards
        log_available_cards(gpu_config(gpu_cards=[9]), probe=FakeProbe(REFERENCE))
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("card 9" in w for w in warnings)


def test_the_banner_never_raises(caplog):
    """It runs inside startup; a sysfs oddity must not stop the service."""
    class Exploding:
        def devices(self):
            raise OSError("sysfs went away")

    import logging

    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        log_available_cards_ok = True
        from cognita.gpu_probe import log_available_cards
        log_available_cards(gpu_config(), probe=Exploding())
    assert log_available_cards_ok


# --------------------------------------------------------------------------
# The ordering the index depends on
# --------------------------------------------------------------------------


def test_discovery_is_ordered_by_pci_address_not_by_card_name(tmp_path):
    """🔴 What makes an index mean the same thing tomorrow.

    Ten cards sort lexicographically as card1, card10, card2 — so under the old
    glob order, adding a tenth card silently renumbered every index a user had
    already written. PCI order is stable and is the identity the index resolves
    to anyway.
    """
    make_card(tmp_path, "card1", total=32 * GIB, pci="0000:c1:00.0")
    make_card(tmp_path, "card2", total=32 * GIB, pci="0000:07:00.0")
    make_card(tmp_path, "card10", total=32 * GIB, pci="0000:03:00.0")

    devices = SysfsAmdProbe(root=tmp_path).devices()
    assert [d.pci_address for d in devices] == [
        "0000:03:00.0", "0000:07:00.0", "0000:c1:00.0",
    ]
    assert resolve_cards(devices, 0).pinned == ["0000:03:00.0"]


def test_reference_machine_ordering_is_unchanged_by_the_pci_sort(tmp_path):
    """card1/card2/card3 sort identically under both rules — deploy is a no-op."""
    make_card(tmp_path, "card1", total=32 * GIB, pci="0000:03:00.0")
    make_card(tmp_path, "card2", total=32 * GIB, pci="0000:07:00.0")
    make_card(tmp_path, "card3", total=32 * GIB, pci="0000:7e:00.0")

    devices = SysfsAmdProbe(root=tmp_path).devices()
    assert [d.sysfs_name for d in devices] == ["card1", "card2", "card3"]
