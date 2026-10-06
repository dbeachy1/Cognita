from __future__ import annotations

import pytest

from cognita.books.read_helpers import spoken_interval


def test_spoken_interval_uses_frozen_tag_spans_with_repeated_text() -> None:
    speech = "[literal] repeat [tag] repeat"
    deletion = "[tag]"
    start = speech.index(deletion)
    end = start + len(deletion)
    spoken = speech[:start] + speech[end:]
    spans = [[start, end]]

    assert spoken_interval(speech, spoken, 0, len(speech), spans) == "[literal] repeat  repeat"
    second_repeat = speech.rindex("repeat")
    assert spoken_interval(speech, spoken, second_repeat, len(speech), spans) == "repeat"
    assert spoken_interval(speech, spoken, start - 2, end + 2, spans) == "t  r"
    assert spoken_interval(speech, spoken, start + 1, end - 1, spans) == ""


def test_spoken_interval_keeps_paragraph_separator_and_multiple_exact_spans() -> None:
    speech = "first [one]\n\nsecond [two]"
    spans = [[speech.index("[one]"), speech.index("[one]") + len("[one]")],
             [speech.index("[two]"), speech.index("[two]") + len("[two]")]]
    spoken = "first \n\nsecond "

    assert spoken_interval(speech, spoken, 0, len(speech), spans) == spoken
    assert spoken_interval(speech, spoken, 11, 21, spans) == "\n\nsecond "


def test_spoken_interval_old_snapshot_requires_identity_without_span_metadata() -> None:
    assert spoken_interval("literal [text]", "literal [text]", 8, 14) == "[text]"
    with pytest.raises(ValueError, match="spans are unavailable"):
        spoken_interval("same [tag] same", "same  same", 0, 15)


def test_spoken_interval_rejects_inconsistent_frozen_spans() -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        spoken_interval("abcdef", "abf", 0, 6, [[2, 5], [4, 5]])
    with pytest.raises(ValueError, match="does not match"):
        spoken_interval("abcdef", "ab", 0, 6, [[2, 5]])
