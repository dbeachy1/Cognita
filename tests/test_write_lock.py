"""The per-project write lock (5.0.2).

The lock itself is testable without PostgreSQL; the race it exists to close is
in tests/test_engine_local.py, which has the store fixture.

Why re-entrancy: the engine's bulk paths hold the lock across a whole operation
while still calling RetrievalCore methods that take it per call. A plain
asyncio.Lock deadlocks on that; the alternative — an unlocked twin of every
write method — is two code paths to keep in step forever.
"""

import asyncio

import pytest

from cognita.retrieval import _ProjectWriteLock


class _FrozenLoopClock:
    """Freeze the running loop's clock so a timeout fires only when the test
    advances it. asyncio schedules every timer against ``loop.time()``, so
    this is a fake clock for acquire_within without touching the product.

    While frozen, the test must keep the loop busy (yield with sleep(0)):
    a loop left idle with only a future timer would select() for the real
    remaining time, find the frozen clock unchanged, and never fire it.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._now = loop.time()

    def __enter__(self) -> "_FrozenLoopClock":
        self._loop.time = lambda: self._now  # instance attribute shadows the method
        return self

    def __exit__(self, *exc_info: object) -> None:
        del self._loop.time  # the class's real clock is back

    def advance(self, seconds: float) -> None:
        self._now += seconds


async def _yield_until(done, *, turns: int) -> None:
    """Give the loop up to `turns` iterations, stopping early once `done()`."""
    for _ in range(turns):
        if done():
            return
        await asyncio.sleep(0)


async def test_same_task_may_take_it_twice():
    lock = _ProjectWriteLock()
    async with lock:
        assert lock.locked()
        async with lock:  # would deadlock on a plain asyncio.Lock
            assert lock.locked()
        # still held by the outer claim
        assert lock.locked()
    assert not lock.locked()


async def test_a_different_task_waits():
    """The point of the lock, not just an implementation detail: re-entrancy is
    scoped to the owning TASK, so the watcher's sync task still blocks."""
    lock = _ProjectWriteLock()
    order: list[str] = []
    released = asyncio.Event()
    waiter_trying = asyncio.Event()

    async def holder():
        async with lock:
            order.append("holder-in")
            # Was a real 50 ms sleep hoping the waiter tried meanwhile. Now
            # holds the lock until the waiter has observably reached it.
            await asyncio.wait_for(waiter_trying.wait(), timeout=5)
            assert lock.locked()
            order.append("holder-out")
        released.set()

    async def waiter():
        await asyncio.sleep(0)  # let holder claim it first
        waiter_trying.set()
        async with lock:
            order.append("waiter-in")

    await asyncio.wait_for(asyncio.gather(holder(), waiter()), timeout=5)
    assert order == ["holder-in", "holder-out", "waiter-in"]
    assert released.is_set()
    assert not lock.locked()


async def test_an_exception_inside_releases_it():
    lock = _ProjectWriteLock()
    with pytest.raises(RuntimeError):
        async with lock:
            async with lock:
                raise RuntimeError("boom")
    assert not lock.locked()
    async with lock:  # not wedged
        pass


async def test_nesting_depth_is_per_lock_not_global():
    a, b = _ProjectWriteLock(), _ProjectWriteLock()
    async with a:
        async with b:
            assert a.locked() and b.locked()
        assert a.locked() and not b.locked()
    assert not a.locked()


async def test_a_blocked_write_reports_busy_rather_than_hanging():
    """C5: index_project holds the lock for a WHOLE corpus walk.

    Measured on a 150-document synthetic corpus against real Postgres: a
    concurrent remove_file waited 1.42s — exactly the rebuild's remaining
    duration. The gateway's read timeout is None, so on a real corpus with a
    real embedder the caller simply hangs until its own client gives up, then
    retries into a stale_file/not_found for a write that had by then succeeded.

    acquire_within bounds that wait. The reindex's own guarantee is untouched:
    it still holds the lock for the whole walk, which is what makes it
    all-or-nothing.
    """
    lock = _ProjectWriteLock()
    holder_has_it = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock:
            holder_has_it.set()
            await asyncio.wait_for(release.wait(), timeout=5)

    task = asyncio.create_task(holder())
    await asyncio.wait_for(holder_has_it.wait(), timeout=5)

    # Was time.monotonic() around a real 0.15 s wait, so a stalled machine
    # could push `waited` past 1.0 and fail a correct lock. Now the loop's
    # own clock -- the one acquire_within's timeout is scheduled against --
    # is frozen and stepped by hand: still waiting at 0.10, gave up at 0.15.
    loop = asyncio.get_running_loop()
    with _FrozenLoopClock(loop) as clock:
        t0 = loop.time()
        attempt = asyncio.create_task(lock.acquire_within(0.15))
        await _yield_until(attempt.done, turns=3)
        clock.advance(0.10)
        await _yield_until(attempt.done, turns=3)
        assert not attempt.done(), "acquire_within gave up before its timeout"
        clock.advance(0.05)
        await _yield_until(attempt.done, turns=20)
        waited = loop.time() - t0
    assert attempt.done(), "acquire_within did not give up once its timeout passed"
    got = attempt.result()
    assert got is False, "acquire_within must give up rather than wait it out"
    assert 0.1 < waited < 1.0, waited

    release.set()
    await asyncio.wait_for(task, timeout=5)
    # and once free, it takes it normally
    assert await lock.acquire_within(1.0) is True
    await lock.release()


async def test_acquire_within_is_re_entrant_for_the_same_task():
    """The same re-entrancy the plain acquire has — an outer bulk claim must be
    able to coexist with the per-call ones underneath it."""
    lock = _ProjectWriteLock()
    async with lock:
        assert await lock.acquire_within(0.05) is True
        await lock.release()
        assert lock.locked()  # still held by the outer claim
    assert not lock.locked()
