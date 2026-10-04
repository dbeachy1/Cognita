"""The self-test plan's pinned hash chain (10.0).

EXPECTED_HASHES are byte-level canaries baked into the plan text: the live
self-test compares each write's returned hash against them, catching any
unintended drift in the write path (normalization, EOL, strip, splice order).

This test recomputes the chain from the SAME pure functions the server uses.
If it fails, either (a) you changed the test content / plan steps — update
EXPECTED_HASHES consciously and note it in the commit — or (b) you changed
splice/normalization behavior, in which case the live canary would have fired
too and you should treat it as a regression until proven intentional.
"""

import base64
import hashlib

from cognita.editing import apply_batch, apply_edit, content_sha256
from cognita.reading import apply_insert
from cognita.selftest import (
    _TEST_CONTENT,
    EXPECTED_HASHES,
    SELF_TEST_CRLF_BASE64,
    SELF_TEST_CRLF_BYTES,
    SELF_TEST_CRLF_SHA256,
    build_self_test_plan,
)


def _chain() -> dict[str, str]:
    # Mirrors the plan exactly. The canonical fixture includes its final LF;
    # current byte-verbatim writes and each subsequent call preserve it.
    t = _TEST_CONTENT
    h = {"H0": content_sha256(t)}
    t = apply_edit(t, "alpha one", "alpha ONE").new_content  # step 5
    h["H1"] = content_sha256(t)
    t = apply_batch(t, [  # step 7
        {"old_str": "alpha two", "new_str": "alpha TWO"},
        {"old_str": "beta one", "new_str": "beta ONE"},
    ]).new_content
    h["H2"] = content_sha256(t)
    t = apply_insert(t, "intro two", "end_of_intro", "Selftest").new_content  # 9
    h["H3"] = content_sha256(t)
    t = apply_insert(t, "alpha three", "end_of_section", "Alpha").new_content  # 10
    h["H4"] = content_sha256(t)
    return h


def test_expected_hash_chain_reproduces():
    assert _chain() == EXPECTED_HASHES


def test_plan_embeds_the_hash_prefixes():
    plan = build_self_test_plan("x.y.z", readonly=False)
    for name, full in EXPECTED_HASHES.items():
        assert full[:16] in plan, f"{name} prefix missing from plan text"
    assert "HASH CHECK" in plan
    assert "REAL regression" in plan


def test_canonical_fixture_and_encoded_call_are_byte_exact():
    raw = _TEST_CONTENT.encode("utf-8")
    assert raw.endswith(b"\n")
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_HASHES["H0"]
    encoded = base64.b64decode(SELF_TEST_CRLF_BASE64, validate=True)
    assert encoded == SELF_TEST_CRLF_BYTES
    assert hashlib.sha256(encoded).hexdigest() == SELF_TEST_CRLF_SHA256
    plan = build_self_test_plan("x.y.z", readonly=False)
    assert SELF_TEST_CRLF_BASE64 in plan
    assert repr(SELF_TEST_CRLF_BYTES.decode("utf-8")) in plan


def test_plan_exercises_move_document():
    from cognita.selftest import TEST_FILE_MOVED

    plan = build_self_test_plan("x.y.z", readonly=False)
    assert "move_document" in plan and TEST_FILE_MOVED in plan  # 4.1 rename step (13b)
    # the writable plan gained a step but the hash chain (steps 3-13) is untouched
    assert _chain() == EXPECTED_HASHES


def test_plan_treats_gpu_throughput_as_informational():
    plan = build_self_test_plan("x.y.z", readonly=False)
    assert "Rates are informational evidence" in plan
    assert "at or above ~45 chunks/s per card" not in plan
