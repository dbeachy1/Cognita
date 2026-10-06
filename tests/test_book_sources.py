from __future__ import annotations

import hashlib

import pytest

from cognita.books.sources import SourceStageError, discard_staged_audio, stage_verified_file


def test_stage_file_hash_checks_and_owned_cleanup(tmp_path):
    source = tmp_path / "source.pcm"
    source.write_bytes(b"\x00\x01\x02\x03")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "staging"
    staged = stage_verified_file(source, root, expected_sha256=digest, source_kind="workspace")
    assert staged.staged_path.read_bytes() == source.read_bytes()
    discard_staged_audio(staged, root)
    assert not staged.staged_path.exists()
    with pytest.raises(SourceStageError) as invalid:
        stage_verified_file(source, root, expected_sha256="0" * 64, source_kind="workspace")
    assert invalid.value.reason == "source_changed"


def test_https_validation_requires_allowlisted_public_host():
    from cognita.books.sources import _validate_https_url

    def resolver(host, port, *_args):
        assert host == "media.example"
        return [(None, None, None, None, ("8.8.8.8", port))]

    host, addresses = _validate_https_url(
        "https://media.example/audio?signature=transient", {"media.example"}, resolver,
    )
    assert host == "media.example" and addresses == ("8.8.8.8",)
    with pytest.raises(SourceStageError) as forbidden:
        _validate_https_url("https://other.example/audio", {"media.example"}, resolver)
    assert forbidden.value.reason == "source_forbidden"

    def private_resolver(host, port, *_args):
        return [(None, None, None, None, ("127.0.0.1", port))]

    with pytest.raises(SourceStageError) as private:
        _validate_https_url("https://media.example/audio", {"media.example"}, private_resolver)
    assert private.value.reason == "source_forbidden"
