"""Nightly pg_dump of the cognita database, with rotation (4.0.1).

The Postgres indexes are derived data (DESIGN-4.0 D4.10) — a dump is a
convenience that turns "reindex everything" recovery into "restore + smart
sync". An example cron entry using local PostgreSQL peer authentication:

    35 3 * * * /home/you/Cognita/.venv/bin/python \
        /home/you/Cognita/scripts/pg-backup.py \
        --out /home/you/Cognita/data/pg-backups \
        >> /home/you/Cognita/logs/pg-backup.log 2>&1
"""

import argparse
import gzip
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="cognita")
    ap.add_argument("--out", required=True, help="backup directory")
    ap.add_argument("--keep", type=int, default=14, help="dumps to retain (0 = unlimited)")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"{args.db}-{stamp}.sql.gz"

    # "never keep a truncated dump" used to be enforced ONLY on the rc != 0
    # branch. If the gzip copy itself raised — disk full, an I/O error — the
    # exception propagated and the partial .sql.gz stayed on disk, looking
    # exactly like a good backup. It is also the NEWEST, so rotation kept it and
    # pruned a real one. And proc was never reaped on that path.
    proc = subprocess.Popen(["pg_dump", args.db], stdout=subprocess.PIPE)
    try:
        with gzip.open(target, "wb") as gz:
            shutil.copyfileobj(proc.stdout, gz)
    except BaseException:
        proc.stdout.close()
        proc.wait()
        target.unlink(missing_ok=True)
        raise
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        target.unlink(missing_ok=True)  # never keep a truncated dump
        print(f"[{stamp}] pg_dump FAILED rc={proc.returncode}", file=sys.stderr)
        return 1
    size_kb = target.stat().st_size // 1024
    pruned = []
    if args.keep > 0:
        dumps = sorted(out_dir.glob(f"{args.db}-*.sql.gz"))
        for old in dumps[: max(0, len(dumps) - args.keep)]:
            old.unlink()
            pruned.append(old.name)
    print(f"[{stamp}] {target.name} written ({size_kb} KB)"
          + (f", pruned {len(pruned)}" if pruned else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
