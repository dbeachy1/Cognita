#!/usr/bin/env python3
"""Set the Cognita admin username + password (interactive).

Prompts for a username, then a password (twice), Argon2id-hashes the password,
and writes admin_username + admin_password_hash to config/cognita.yaml. The
plaintext is NEVER stored, echoed, or written anywhere — only the hash lands on
disk (same discipline as the project bearer tokens). Other config keys are
preserved.

Don't run this directly — use the wrappers, which ALSO restart the server so the
change takes effect:
    scripts/set-admin-credentials.sh     (Linux / kei)
    scripts\\set-admin-credentials.bat    (Windows)

Fresh installs have no password. Run this before enabling OAuth or exposing the
admin UI beyond loopback.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

import yaml
from argon2 import PasswordHasher

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "cognita.yaml"
MAX_STDIN_JSON_CHARS = 64 * 1024


def _load_config(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("config is not a YAML mapping")
    return loaded


def _write_credentials(config_path: Path, data: dict, username: str, password: str) -> None:
    data["admin_username"] = username
    data["admin_password_hash"] = PasswordHasher().hash(password)
    data.pop("admin_password_sha256", None)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(config_path)


def _read_stdin_credentials() -> tuple[str, str] | None:
    payload = sys.stdin.read(MAX_STDIN_JSON_CHARS + 1)
    if len(payload) > MAX_STDIN_JSON_CHARS:
        print("Aborted: credential input is too large.", file=sys.stderr)
        return None
    try:
        credentials = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        print("Aborted: credential input must be a JSON object.", file=sys.stderr)
        return None
    if (not isinstance(credentials, dict) or set(credentials) != {"username", "password"}
            or not isinstance(credentials["username"], str)
            or not isinstance(credentials["password"], str)):
        print("Aborted: credential input must contain only string username and password fields.",
              file=sys.stderr)
        return None
    username = credentials["username"].strip()
    password = credentials["password"]
    if not username or not password:
        print("Aborted: username and password must be non-empty.", file=sys.stderr)
        return None
    return username, password


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Set the Cognita admin username + password (stores only the password hash)."
    )
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config file to update")
    ap.add_argument("--stdin-json", action="store_true",
                    help="read one JSON object with username and password from standard input")
    args = ap.parse_args()

    try:
        data = _load_config(args.config)
    except ValueError:
        print(f"Aborted: {args.config} is not a YAML mapping.", file=sys.stderr)
        return 2

    if args.stdin_json:
        credentials = _read_stdin_credentials()
        if credentials is None:
            return 2
        username, pw = credentials
        _write_credentials(args.config, data, username, pw)
        # Avoid printing either credential in command output or release logs.
        print("Admin credentials updated; only an Argon2id verifier was written.")
        return 0

    current_user = str(data.get("admin_username", "admin"))
    username = input(f"Admin username [{current_user}]: ").strip() or current_user

    pw = getpass.getpass("New admin password: ")
    if not pw:
        print("Aborted: empty password.", file=sys.stderr)
        return 2
    if pw != getpass.getpass("Confirm password: "):
        print("Aborted: passwords do not match.", file=sys.stderr)
        return 2

    _write_credentials(args.config, data, username, pw)

    print(f"\nAdmin credentials updated (user: {username}) -> {args.config}")
    print("Only an Argon2id verifier was written; the plaintext was not stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
