from __future__ import annotations

import asyncio
import hashlib

import pytest

from cognita.books.sources import (
    SourceStageError,
    _PinnedPublicBackend,
    discard_staged_audio,
    stage_verified_file,
)


def test_stage_file_hash_checks_and_owned_cleanup(tmp_path):
    source = tmp_path / "source.pcm"
    source.write_bytes(b"\x00\x01\x02\x03")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "staging"
    staged = stage_verified_file(source, root, expected_sha256=digest, source_kind="workspace",
                                 max_bytes=4 * 1024**3)
    assert staged.staged_path.read_bytes() == source.read_bytes()
    discard_staged_audio(staged, root)
    assert not staged.staged_path.exists()
    with pytest.raises(SourceStageError) as invalid:
        stage_verified_file(source, root, expected_sha256="0" * 64, source_kind="workspace",
                            max_bytes=4 * 1024**3)
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

class _Response:
    def __init__(self, status, headers=(), chunks=(), error=None):
        self.status, self.headers, self._chunks = status, list(headers), list(chunks)
        self._error = error

    async def aiter_stream(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

class _Stream:
    def __init__(self, response): self.response = response
    async def __aenter__(self): return self.response
    async def __aexit__(self, *args): return False

class _Pool:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.closed = False

    def stream(self, *args, **kwargs):
        return _Stream(next(self.responses))

    async def aclose(self):
        self.closed = True


class _RecordingBackend:
    def __init__(self):
        self.connections = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections.append((host, port, timeout, local_address, socket_options))
        return object()

    async def sleep(self, seconds):
        return None


@pytest.mark.asyncio
async def test_https_connection_uses_vetted_numeric_address():
    backend = _RecordingBackend()

    def resolver(host, port, *_args):
        assert host == "media.example"
        return [(None, None, None, None, ("8.8.8.8", port))]

    pinned = _PinnedPublicBackend(resolver=resolver, backend=backend)
    await pinned.connect_tcp("media.example", 443, 5.0)
    assert backend.connections[0][0:3] == ("8.8.8.8", 443, 5.0)


@pytest.mark.asyncio
async def test_https_stage_streams_pinned_bytes_and_cleans_failed_stages(tmp_path):
    from cognita.books.sources import stage_https_audio_source
    payload = b"native-https-audio"
    digest = hashlib.sha256(payload).hexdigest()
    pools = []
    def factory(_backend, _timeout):
        pool = _Pool([_Response(200, [(b"content-length", str(len(payload)).encode())], [payload])])
        pools.append(pool)
        return pool
    def resolver(_host, port, *_args): return [(None, None, None, None, ("8.8.8.8", port))]
    staged = await stage_https_audio_source("https://media.example/result", tmp_path, allowed_hosts=["media.example"], max_bytes=100, expected_sha256=digest, resolver=resolver, _pool_factory=factory)
    assert staged.staged_path.read_bytes() == payload and pools[0].closed
    discard_staged_audio(staged, tmp_path)

    def short_factory(_backend, _timeout): return _Pool([_Response(200, [(b"content-length", b"20")], [b"short"])])
    with pytest.raises(SourceStageError) as short:
        await stage_https_audio_source("https://media.example/result", tmp_path, allowed_hosts=["media.example"], max_bytes=100, resolver=resolver, _pool_factory=short_factory)
    assert short.value.reason == "source_changed"

    def quota_factory(_backend, _timeout): return _Pool([_Response(200, [], [b"too-large"])])
    with pytest.raises(SourceStageError) as quota:
        await stage_https_audio_source("https://media.example/result", tmp_path, allowed_hosts=["media.example"], max_bytes=2, resolver=resolver, _pool_factory=quota_factory)
    assert quota.value.reason == "quota_exceeded"

    def interrupted_factory(_backend, _timeout):
        return _Pool([_Response(200, [], [b"partial"], error=RuntimeError("socket closed"))])
    with pytest.raises(SourceStageError) as interrupted:
        await stage_https_audio_source("https://media.example/result", tmp_path, allowed_hosts=["media.example"], max_bytes=100, resolver=resolver, _pool_factory=interrupted_factory)
    assert interrupted.value.reason == "source_unavailable"
    assert not list(tmp_path.glob(".cognita-book-source-*"))


@pytest.mark.asyncio
async def test_https_stage_revalidates_redirect_and_total_deadline(tmp_path):
    from cognita.books.sources import stage_https_audio_source
    def resolver(host, port, *_args):
        address = "127.0.0.1" if host == "private.example" else "8.8.8.8"
        return [(None, None, None, None, (address, port))]
    def redirect(_backend, _timeout):
        return _Pool([_Response(302, [(b"location", b"https://private.example/x")])])
    with pytest.raises(SourceStageError) as denied:
        await stage_https_audio_source("https://media.example/a", tmp_path, allowed_hosts=["media.example", "private.example"], max_bytes=100, resolver=resolver, _pool_factory=redirect)
    assert denied.value.reason == "source_forbidden"
    clock = iter([0.0, 2.0])
    with pytest.raises(SourceStageError) as timed:
        await stage_https_audio_source("https://media.example/a", tmp_path, allowed_hosts=["media.example"], max_bytes=100, resolver=resolver, _pool_factory=lambda *_: _Pool([_Response(200, [], [b"x"])]), timeout_seconds=1, _clock=lambda: next(clock))
    assert timed.value.reason == "source_timeout"


@pytest.mark.asyncio
async def test_https_cancellation_closes_pool_and_removes_owned_stage(tmp_path):
    from cognita.books.sources import stage_https_audio_source

    started = asyncio.Event()

    class WaitingResponse(_Response):
        async def aiter_stream(self):
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    pools = []

    def factory(_backend, _timeout):
        pool = _Pool([WaitingResponse(200)])
        pools.append(pool)
        return pool

    def resolver(_host, port, *_args):
        return [(None, None, None, None, ("8.8.8.8", port))]

    task = asyncio.create_task(stage_https_audio_source(
        "https://media.example/result?signature=transient", tmp_path,
        allowed_hosts=["media.example"], max_bytes=1024,
        resolver=resolver, _pool_factory=factory,
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert pools and pools[0].closed
    assert not list(tmp_path.glob(".cognita-book-source-*"))
