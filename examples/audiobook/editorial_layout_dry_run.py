"""Print a non-mutating legacy Prologue/ChapterNN layout migration plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from cognita.books.editorial_mapping import dry_run_editorial_layout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--layout", default="Project Files/Book_Layout.json")
    args = parser.parse_args()
    root = args.project_root.resolve(strict=True)
    layout = json.loads((root / args.layout).read_text(encoding="utf-8"))
    print(json.dumps(dry_run_editorial_layout(root, layout), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
