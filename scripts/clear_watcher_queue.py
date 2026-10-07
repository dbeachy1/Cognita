#!/usr/bin/env python3
"""Clear a Cognita project's pending file-watcher queue through Admin HTTP."""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

DEFAULT_ADMIN_URL = "http://127.0.0.1:8676"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1024 * 1024


class AdminRequestError(Exception):
    """A safe, credential-free description of a failed Admin request."""


def _request_json(opener, url: str, *, method: str, payload: dict,
                  headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise AdminRequestError(f"Admin returned HTTP {exc.code}.") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise AdminRequestError(f"Could not reach Admin ({type(exc).__name__}).") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AdminRequestError("Admin response exceeded the 1 MiB limit.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AdminRequestError("Admin returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise AdminRequestError("Admin returned an unexpected response.")
    return result


def clear_queue(base_url: str, name: str, username: str, password: str) -> dict:
    """Log into Admin, then clear the named project's queue using its CSRF token."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise AdminRequestError("Admin URL must be an absolute HTTP or HTTPS URL without embedded credentials.")
    root = base_url.rstrip("/")
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    login = _request_json(
        opener, f"{root}/api/login", method="POST",
        payload={"username": username, "password": password},
    )
    if not login.get("ok"):
        raise AdminRequestError("Admin login was rejected.")
    csrf = next((cookie.value for cookie in cookie_jar
                 if cookie.name == "cognita_csrf"), None)
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return _request_json(
        opener, f"{root}/api/projects/{quote(name, safe='')}/watcher/clear-queue",
        method="POST", payload={}, headers=headers,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clear a project's Cognita file-watcher queue.")
    parser.add_argument("project", help="exact Cognita project name")
    parser.add_argument(
        "--url", default=os.environ.get("COGNITA_ADMIN_URL", DEFAULT_ADMIN_URL),
        help="Admin base URL (default: COGNITA_ADMIN_URL or %(default)s)",
    )
    parser.add_argument("--username", default=os.environ.get("COGNITA_ADMIN_USERNAME", "admin"))
    args = parser.parse_args(argv)
    password = os.environ.get("COGNITA_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("Cognita Admin password: ")
    try:
        result = clear_queue(args.url, args.project, args.username, password)
    except AdminRequestError as exc:
        print(f"Could not clear watcher queue: {exc}", file=sys.stderr)
        return 1
    print(
        f"Cleared {result.get('cleared_paths', 0)} queued paths for "
        f"{result.get('project', args.project)}; "
        f"active batch cancelled: {'yes' if result.get('active_cancelled') else 'no'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
