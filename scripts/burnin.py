"""The M5 burn-in gauntlet (DESIGN-4.0-vector-engine.md §9) — run ON kei.

Replicates the 3.0.2 forensic acceptance conditions against the 4.0 stack:

  churn    10-minute 4-way concurrent search hammer with ~205 watcher-firing
           file touches (the R3 conditions; 3.x benchmark: 9,747 searches,
           0 failures)
  kill     SIGKILL the whole server mid-reindex x20; the Postgres index must
           be consistent after every kill (the residue class must not exist)
  reindex  force full re-embeds x10 back-to-back; counts identical every time
  restart  graceful restart x10; health + a live search must pass every time

The script owns its server instance (spawn/kill/restart) — do not point it at
a server you care about. Usage (from the repo root on kei):

    COGNITA_TEST_API_KEY=<key> .venv/bin/python scripts/burnin.py --project BURNIN \
        --docs ~/cognita-burnin-docs --phase all
"""

import argparse
import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cognita.store import Store  # noqa: E402

MCP_PORT = 8875
BASE = f"http://127.0.0.1:{MCP_PORT}"
DSN = os.environ.get("COGNITA_PG_DSN", "postgresql:///cognita")

QUERY_POOL = [
    "ROCm build procedure", "PCIe slots layout", "rebuild procedure order",
    "koboldcpp launch flags", "vulkan vs rocm performance", "gfx1201",
    "sillytavern world book", "image generation models", "vocalis tts setup",
    "chatterbox server phase", "kv cache q8_0", "power supply headroom",
    "zfs pool restore", "claude desktop mcp", "co-author agent scans",
    "moe inference box plan", "dual gpu bring-up", "hardware details slots",
    "software stack phases", "persona setup", "voice cloning", "todos open questions",
    "system config services", "nas rebuild readme", "local rag setup",
    "characters template", "editorial scan engine", "context window fits",
    "rocm pytorch wheel", "ubuntu 26.04 support", "amdgpu driver stack",
    "temperature monitoring", "model quantization quality", "sampler settings",
    "world info entries", "inference throughput", "case fans airflow",
    "bios settings", "memory training", "boot drive layout",
]


class ServerManager:
    def __init__(self, repo: Path):
        self.repo = repo
        self.proc: subprocess.Popen | None = None
        self.log = open("/tmp/cognita-burnin-server.log", "ab")

    def start(self, timeout: float = 90.0) -> float:
        env = os.environ.copy()
        env.update({
            "COGNITA_PG_DSN": DSN,
            "COGNITA_MCP_PORT": str(MCP_PORT),
            "COGNITA_ADMIN_PORT": "8876",
        })
        env.setdefault("COGNITA_MODELS_CACHE_DIR", str(Path.home() / "Cognita" / "models_cache"))
        self.proc = subprocess.Popen(
            [str(self.repo / ".venv/bin/python"), "-m", "cognita", "serve", "--test"],
            cwd=str(self.repo), env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        t0 = time.monotonic()
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{BASE}/healthz", timeout=2.0)
                if r.status_code == 200:
                    return time.monotonic() - t0
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited rc={self.proc.returncode} during startup")
            time.sleep(0.25)
        raise RuntimeError("server did not become healthy in time")

    def sigkill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=15)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def mcp_call(client: httpx.Client, token: str, tool: str, args: dict, timeout=60.0) -> dict:
    r = client.post(f"{BASE}/mcp", timeout=timeout,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"rpc error: {body['error']}")
    return json.loads(body["result"]["content"][0]["text"])


async def check_consistency(project: str) -> tuple[list[str], int, int]:
    store = Store(DSN, embedding_dimensions=1024)
    await store.connect()
    try:
        violations = await store.check_consistency(project)
        stats = await store.stats(project)
        return violations, stats.documents, stats.chunks
    finally:
        await store.close()


def wait_reindex_done(client, token, timeout=1800.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = mcp_call(client, token, "get_reindex_status", {})
        if not status["reindex"].get("active"):
            return status["reindex"]
        time.sleep(1.0)
    raise RuntimeError("reindex did not finish in time")


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


async def phase_churn(server, token, project, docs: Path, minutes: float, touches: int):
    """R3 replica: 4-way search hammer + watcher-firing touches, N minutes."""
    stop_at = time.monotonic() + minutes * 60
    counts = {"searches": 0, "failures": 0, "touches": 0}
    failures: list[str] = []
    md_files = sorted(docs.glob("*.md"))

    async def hammer(worker_id: int):
        async with httpx.AsyncClient(timeout=60.0,
                                     headers={"Authorization": f"Bearer {token}"}) as c:
            i = worker_id
            while time.monotonic() < stop_at:
                query = QUERY_POOL[i % len(QUERY_POOL)]
                alpha = (0.0, 0.3, 0.3, 1.0)[i % 4]
                i += 1
                try:
                    r = await c.post(f"{BASE}/mcp", json={
                        "jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": "search_knowledge",
                                   "arguments": {"query": query, "hybrid_alpha": alpha}}})
                    payload = json.loads(r.json()["result"]["content"][0]["text"])
                    if payload.get("status") not in ("success", "no_results"):
                        raise RuntimeError(f"bad status: {payload}")
                    counts["searches"] += 1
                except Exception as exc:
                    counts["failures"] += 1
                    if len(failures) < 5:
                        failures.append(f"{query!r}: {type(exc).__name__}: {exc}")

    async def toucher():
        interval = (minutes * 60) / touches
        n = 0
        while time.monotonic() < stop_at:
            target = md_files[n % len(md_files)]
            with open(target, "a", encoding="utf-8") as f:
                f.write(f"\nchurn touch {n} at {time.time():.0f}\n")
            counts["touches"] += 1
            n += 1
            await asyncio.sleep(interval)

    await asyncio.gather(*(hammer(w) for w in range(4)), toucher())
    async with httpx.AsyncClient(timeout=60.0,
                                 headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"{BASE}/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_index_stats", "arguments": {}}})
        stats = json.loads(r.json()["result"]["content"][0]["text"])["stats"]
    violations, ndocs, nchunks = await check_consistency(project)
    return {
        **counts,
        "rate_per_s": round(counts["searches"] / (minutes * 60), 1),
        "cache": stats["query_cache"],
        "consistency_violations": violations,
        "docs": ndocs, "chunks": nchunks,
        "failure_samples": failures,
    }


async def phase_kill(server, token, project, kills: int):
    results = []
    client = httpx.Client()
    for i in range(kills):
        mcp_call(client, token, "reindex_documents", {"full_rebuild": True})
        delay = random.uniform(3.0, 25.0)
        time.sleep(delay)
        server.sigkill()
        violations, ndocs, nchunks = await check_consistency(project)
        ok = not violations
        results.append({"kill": i + 1, "after_s": round(delay, 1), "ok": ok,
                        "docs": ndocs, "chunks": nchunks,
                        "violations": violations})
        print(f"  kill {i + 1}/{kills}: killed {delay:.1f}s into reindex -> "
              f"{'CONSISTENT' if ok else 'VIOLATIONS: ' + str(violations)} "
              f"({ndocs} docs / {nchunks} chunks)", flush=True)
        server.start()
    # Let the final boot reindex finish, then one clean full pass as the capstone
    wait_reindex_done(client, token)
    mcp_call(client, token, "reindex_documents", {"full_rebuild": True})
    final = wait_reindex_done(client, token)
    violations, ndocs, nchunks = await check_consistency(project)
    return {"kills": results, "all_consistent": all(r["ok"] for r in results),
            "final_reindex": final.get("last_result"),
            "final_docs": ndocs, "final_chunks": nchunks,
            "final_violations": violations}


async def phase_reindex(server, token, project, runs: int):
    client = httpx.Client()
    wait_reindex_done(client, token)
    counts = []
    for i in range(runs):
        mcp_call(client, token, "reindex_documents", {"full_rebuild": True})
        result = wait_reindex_done(client, token)
        violations, ndocs, nchunks = await check_consistency(project)
        counts.append((ndocs, nchunks, tuple(violations)))
        print(f"  reindex {i + 1}/{runs}: {result.get('last_result')} -> "
              f"{ndocs} docs / {nchunks} chunks, violations={violations}", flush=True)
    stable = len(set(counts)) == 1 and not counts[0][2]
    return {"runs": runs, "stable_counts": stable, "counts": [c[:2] for c in counts]}


async def phase_restart(server, token, project, restarts: int):
    client = httpx.Client()
    results = []
    for i in range(restarts):
        server.stop()
        ready_s = server.start()
        # a real (uncached, un-warmed) search must work immediately
        payload = mcp_call(client, token, "search_knowledge",
                           {"query": random.choice(QUERY_POOL), "max_results": 3},
                           timeout=120.0)
        ok = payload.get("status") in ("success", "no_results")
        results.append({"restart": i + 1, "ready_s": round(ready_s, 1), "search_ok": ok})
        print(f"  restart {i + 1}/{restarts}: ready in {ready_s:.1f}s, "
              f"search {'OK' if ok else 'FAILED'}", flush=True)
    return {"restarts": results, "all_ok": all(r["search_ok"] for r in results)}


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--phase", default="all",
                    choices=["all", "churn", "kill", "reindex", "restart"])
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--touches", type=int, default=205)
    ap.add_argument("--kills", type=int, default=20)
    ap.add_argument("--reindexes", type=int, default=10)
    ap.add_argument("--restarts", type=int, default=10)
    args = ap.parse_args()

    token = os.environ.get("COGNITA_TEST_API_KEY", "")
    if not token:
        ap.error("COGNITA_TEST_API_KEY must contain the test-mode static API key")

    repo = Path(__file__).resolve().parents[1]
    docs = Path(args.docs).expanduser()
    server = ServerManager(repo)
    # Scope the sweep to THIS repo's venv. A bare "cognita serve" pattern also
    # matches the PRODUCTION server (learned the hard way on 2026-07-09: a
    # broad pkill took prod down for hours).
    subprocess.run(["pkill", "-f", f"{repo}/.venv/bin/python -m cognita serve"], check=False)
    time.sleep(2)
    report: dict = {}
    print(f"starting server for burn-in (phase={args.phase}) ...", flush=True)
    ready = server.start()
    print(f"server ready in {ready:.1f}s", flush=True)
    client = httpx.Client()
    wait_reindex_done(client, token)  # let the boot sync settle first

    try:
        if args.phase in ("all", "churn"):
            print(f"\n=== CHURN: {args.minutes:.0f} min, 4 workers, "
                  f"{args.touches} touches ===", flush=True)
            report["churn"] = await phase_churn(server, token, args.project,
                                                docs, args.minutes, args.touches)
            print(json.dumps(report["churn"], indent=2))
        if args.phase in ("all", "kill"):
            print(f"\n=== KILL-MID-INDEX x{args.kills} ===", flush=True)
            report["kill"] = await phase_kill(server, token, args.project, args.kills)
            print(json.dumps({k: v for k, v in report["kill"].items() if k != "kills"},
                             indent=2))
        if args.phase in ("all", "reindex"):
            print(f"\n=== FORCE-REINDEX x{args.reindexes} ===", flush=True)
            report["reindex"] = await phase_reindex(server, token, args.project,
                                                    args.reindexes)
            print(json.dumps(report["reindex"], indent=2))
        if args.phase in ("all", "restart"):
            print(f"\n=== RESTART x{args.restarts} ===", flush=True)
            report["restart"] = await phase_restart(server, token, args.project,
                                                    args.restarts)
            print(json.dumps(report["restart"], indent=2))
    finally:
        server.stop()

    Path("/tmp/cognita-burnin-report.json").write_text(json.dumps(report, indent=2))
    print("\nreport written to /tmp/cognita-burnin-report.json")


if __name__ == "__main__":
    asyncio.run(main())
