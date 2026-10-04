#!/usr/bin/env python3
"""Provision Cognita's bounded, synthetic persistent ``Self-Test`` fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cognita.selftest_fixtures import FixtureProvisionError, provision_self_test


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    connector_mode = parser.add_mutually_exclusive_group(required=True)
    connector_mode.add_argument("--connectors", type=Path)
    connector_mode.add_argument("--defer-connector-check", action="store_true")
    args = parser.parse_args()
    try:
        result = provision_self_test(
            documents_dir=args.documents_dir,
            data_dir=args.data_dir,
            registry_path=args.registry,
            connectors_path=args.connectors,
        )
    except FixtureProvisionError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": ("SELFTEST_FIXTURES_READY_CONNECTOR_DEFERRED"
                   if args.defer_connector_check else "SELFTEST_FIXTURES_READY"),
        "project_created": result.project_created,
        "copied_count": len(result.copied),
        "verified_count": len(result.verified),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
