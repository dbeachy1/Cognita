"""Admin auth tests: form login + signed session cookie, and the exposed-bind
startup guard.

Covers the three states from admin_auth.py — open (loopback, no password),
enforced (password set → login required), and refused (non-loopback + no
password) — the session-token primitives, and proof that credentials come only
from the config file, never the environment.
"""

import os
from pathlib import Path
from unittest import mock

import pytest
from httpx import ASGITransport, AsyncClient

import cognita.admin_auth as aa
from cognita.admin_api import create_admin_app
from cognita.admin_auth import (
    SESSION_COOKIE,
    admin_auth_configured,
    allowed_admin_hosts,
    is_loopback,
    issue_session_token,
    read_session_user,
)
from cognita.config import CognitaConfig, load_config
from cognita.registry import Registry
from cognita.tokens import hash_token

PW = "correct horse battery staple"


def _config(tmp_path: Path, **over) -> CognitaConfig:
    over.setdefault("admin_allowed_hosts", ["*"])  # ASGI client sends Host: test
    return CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        data_root=tmp_path / "data",
        **over,
    )


def _app(config: CognitaConfig):
    registry = Registry(config.registry_path)
    return create_admin_app(config, registry)


async def _client(app, **kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


# ------------------------------------------------------------------ is_loopback


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("127.5.5.5", True),
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("kei", False),
        ("cognita.example.com", False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_admin_auth_configured(tmp_path):
    """NO password ships, so the startup guard can actually fire.

    This used to assert the opposite — that the config ships with
    sha256("welcome") — which is what made _require_admin_auth_if_exposed
    unreachable: it refuses a non-loopback bind only when no password is set,
    and one was ALWAYS set. An exposed admin surface with credentials printed
    in the repo therefore started cleanly, which is the exact condition the
    guard exists to refuse.
    """
    assert admin_auth_configured(_config(tmp_path)) is False
    assert admin_auth_configured(_config(tmp_path, admin_password_sha256=hash_token(PW))) is True


# --------------------------------------------------------------- open (no auth)


async def test_open_mode_no_login_required(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=""))  # auth disabled
    async with await _client(app) as c:
        assert (await c.get("/api/projects")).status_code == 200
        page = (await c.get("/")).text
        assert "projects-body" in page  # app served directly
        assert 'id="test-documents-path"' in page
        assert 'id="documents-path-status"' in page
        assert "/api/projects/path-info" in (await c.get("/static/app.js")).text
        assert (await c.get("/api/session")).json()["auth_required"] is False


async def test_documents_path_probe_requires_session_and_csrf(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_bytes(b"hello")
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    payload = {"documents_dir": str(docs)}
    async with await _client(app) as c:
        assert (await c.post("/api/projects/path-info", json=payload)).status_code == 401
        assert (await c.post(
            "/api/login", json={"username": "admin", "password": PW}
        )).status_code == 200
        assert (await c.post("/api/projects/path-info", json=payload)).status_code == 403
        csrf = c.cookies.get("cognita_csrf")
        checked = await c.post(
            "/api/projects/path-info",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
    assert checked.status_code == 200
    assert checked.json()["file_count"] == 1
    assert checked.json()["total_bytes"] == 5


# --------------------------------------------------------------- login flow


async def test_protected_without_cookie_is_401(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        r = await c.get("/api/projects")
    assert r.status_code == 401


async def test_login_page_follows_system_theme_until_one_is_picked(tmp_path):
    """The first sign-in page has no theme cookie yet: it must not force light
    on a dark desktop (14.2.5). A picked theme still wins."""
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        first = await c.get("/")
        c.cookies.set("cognita_theme", "dark")
        picked = await c.get("/")
    assert "Sign in" in first.text
    assert "data-theme" not in first.text.split("<head>", 1)[0]
    assert '<html lang="en" data-theme="dark">' in picked.text


async def test_login_wrong_password_denied(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
        assert (await c.get("/api/projects")).status_code == 401  # no cookie granted


async def test_login_wrong_username_denied(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        r = await c.post("/api/login", json={"username": "root", "password": PW})
        assert r.status_code == 401


async def test_login_then_access_then_logout(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        assert r.status_code == 200
        assert SESSION_COOKIE in c.cookies  # signed cookie now in the jar
        # the jar auto-sends it → protected routes work
        assert (await c.get("/api/projects")).status_code == 200
        assert (await c.get("/api/session")).json() == {
            "auth_required": True,
            "authenticated": True,
            "username": "admin",
        }
        # logout clears the cookie → back to 401
        assert (await c.post("/api/logout")).status_code == 200
        assert (await c.get("/api/projects")).status_code == 401


async def test_existing_session_bootstraps_csrf_cookie_on_reload(tmp_path):
    """Sessions issued before CSRF rollout can still submit Admin mutations."""
    docs = tmp_path / "docs"
    docs.mkdir()
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": PW})).status_code == 200
        # Reproduce a session that predates the CSRF cookie: auth still works,
        # but the browser has no token for app.js to copy into the header.
        csrf_cookie = next(cookie for cookie in c.cookies.jar if cookie.name == "cognita_csrf")
        c.cookies.jar.clear(
            domain=csrf_cookie.domain, path=csrf_cookie.path, name=csrf_cookie.name,
        )
        assert c.cookies.get("cognita_csrf") is None
        reloaded = await c.get("/")
        assert reloaded.status_code == 200
        assert "projects-body" in reloaded.text
        assert any(header.startswith("cognita_csrf=") for header in reloaded.headers.get_list("set-cookie"))
        csrf = c.cookies.get("cognita_csrf")
        assert csrf
        assert (await c.get("/api/projects")).status_code == 200
        created = await c.post(
            "/api/projects",
            json={"name": "Reloaded", "documents_dir": str(docs)},
            headers={"X-CSRF-Token": csrf},
        )
    assert created.status_code == 201


async def test_custom_username_login(tmp_path):
    cfg = _config(tmp_path, admin_username="operator", admin_password_sha256=hash_token(PW))
    async with await _client(_app(cfg)) as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": PW})).status_code == 401
        assert (await c.post("/api/login", json={"username": "operator", "password": PW})).status_code == 200
        assert (await c.get("/api/projects")).status_code == 200


async def test_browse_is_protected(tmp_path):
    """The filesystem lister must not be reachable without a session."""
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        assert (await c.get("/api/browse")).status_code == 401


# --------------------------------------------------------------- index gate


async def test_index_serves_login_when_unauthenticated(tmp_path):
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "login-form" in r.text  # the login page, not the app
        await c.post("/api/login", json={"username": "admin", "password": PW})
        assert "projects-body" in (await c.get("/")).text  # now the app


# --------------------------------------------------------------- session tokens


def test_session_token_roundtrip(tmp_path):
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    assert read_session_user(cfg, issue_session_token(cfg)) == cfg.admin_username


def test_session_token_tampered_or_missing_rejected(tmp_path):
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    body = issue_session_token(cfg).partition(".")[0]
    assert read_session_user(cfg, body + ".AAAA") is None
    assert read_session_user(cfg, "garbage") is None
    assert read_session_user(cfg, None) is None


def test_session_token_password_change_invalidates(tmp_path):
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    tok = issue_session_token(cfg)
    changed = _config(tmp_path, admin_password_sha256=hash_token("different"))
    assert read_session_user(changed, tok) is None


def test_session_token_username_change_invalidates(tmp_path):
    cfg = _config(tmp_path, admin_username="operator", admin_password_sha256=hash_token(PW))
    tok = issue_session_token(cfg)  # signed with same pw hash → signature stays valid
    renamed = _config(tmp_path, admin_username="eve", admin_password_sha256=hash_token(PW))
    assert read_session_user(renamed, tok) is None  # but the bound username no longer matches


def test_session_token_expiry(tmp_path, monkeypatch):
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    monkeypatch.setattr(aa.time, "time", lambda: 1000.0)
    tok = issue_session_token(cfg)  # exp = 1000 + 30d
    assert read_session_user(cfg, tok) == cfg.admin_username  # still valid "now"
    monkeypatch.setattr(aa.time, "time", lambda: 1000.0 + 40 * 86400)  # 40 days on
    assert read_session_user(cfg, tok) is None


# --------------------------------------------------------------- startup guard


def test_startup_guard_refuses_exposed_open(tmp_path):
    from cognita.__main__ import _require_admin_auth_if_exposed

    cfg = _config(tmp_path, admin_host="0.0.0.0", admin_password_sha256="")  # exposed, no password
    with pytest.raises(SystemExit):
        _require_admin_auth_if_exposed(cfg)


def test_startup_guard_allows_exposed_with_password(tmp_path):
    from cognita.__main__ import _require_admin_auth_if_exposed

    cfg = _config(tmp_path, admin_host="0.0.0.0", admin_password_sha256=hash_token(PW))
    _require_admin_auth_if_exposed(cfg)  # must not raise


def test_startup_guard_allows_loopback_open(tmp_path):
    from cognita.__main__ import _require_admin_auth_if_exposed

    _require_admin_auth_if_exposed(_config(tmp_path, admin_password_sha256=""))  # loopback + no auth: fine


# ------------------------------------------ credentials come ONLY from the config


def test_env_does_not_override_config_credentials(tmp_path, monkeypatch):
    """A stale credential env var must never override the stored config values."""
    cfg_file = tmp_path / "cognita.yaml"
    cfg_file.write_text(
        f'admin_username: "operator"\nadmin_password_sha256: "{hash_token(PW)}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("COGNITA_ADMIN_PASSWORD", "welcome")
    monkeypatch.setenv("COGNITA_ADMIN_PASSWORD_SHA256", hash_token("welcome"))
    monkeypatch.setenv("COGNITA_ADMIN_USERNAME", "attacker")
    cfg = load_config(cfg_file)
    assert cfg.admin_username == "operator"
    assert cfg.admin_password_sha256 == hash_token(PW)


def test_env_plaintext_password_is_ignored(tmp_path, monkeypatch):
    """COGNITA_ADMIN_PASSWORD never seeds the hash — and no default fills in."""
    monkeypatch.setenv("COGNITA_ADMIN_PASSWORD", PW)
    cfg = load_config(tmp_path / "no-such-config.yaml")
    assert cfg.admin_password_sha256 == ""  # not PW, and not a shipped default


# ------------------------------------------------ 5.1: the session key is per-install


def test_session_key_is_not_derivable_from_repo_defaults(tmp_path):
    """A cookie minted from config defaults must not verify against a real install.

    The signing key used to be sha256("cognita.session.v1|" + admin_password_sha256)
    and nothing else — and admin_password_sha256 shipped with a default. Anyone
    holding this repo could therefore mint a valid admin cookie offline for any
    install still on that default: no password, no login request, no rate limit
    to defeat, straight past _gate into token minting and /api/browse.
    """
    attacker = CognitaConfig(data_root=tmp_path / "attacker")
    forged = issue_session_token(attacker)

    victim = _config(tmp_path, admin_password_sha256=hash_token(PW), admin_host="0.0.0.0")
    assert read_session_user(victim, forged) is None


def test_session_key_persists_across_config_reloads(tmp_path):
    """A restart must not invalidate every live session..."""
    cfg_a = _config(tmp_path, admin_password_sha256=hash_token(PW))
    token = issue_session_token(cfg_a)

    aa._SESSION_KEYS.clear()  # simulate a fresh process reading the same data_root
    cfg_b = _config(tmp_path, admin_password_sha256=hash_token(PW))
    assert read_session_user(cfg_b, token) == cfg_b.admin_username


def test_a_transient_read_error_does_not_destroy_the_session_key(tmp_path):
    """One unreadable moment used to log every admin out permanently (5.6.3).

    An OSError from read_bytes() fell through to the generate branch, whose
    open() carries O_TRUNC — so a virus scanner holding the file for a few
    milliseconds on Windows REPLACED the durable secret, and every outstanding
    cookie stopped verifying for good. The write-failure path was fail-safe; this
    one was the opposite, which is backwards, because a read failure is the
    transient case.

    Asserted on the FILE, not just on the return value: the bug was a side
    effect, and a test that only checked the key it got back would have passed
    while the secret on disk was already gone.
    """
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    token = issue_session_token(cfg)
    key_file = Path(cfg.data_root) / aa.SESSION_KEY_FILENAME
    original = key_file.read_bytes()

    aa._SESSION_KEYS.clear()
    real_read_bytes = Path.read_bytes

    def failing_read(self, *args, **kwargs):
        if self.name == aa.SESSION_KEY_FILENAME:
            raise OSError(13, "briefly locked by something else")
        return real_read_bytes(self, *args, **kwargs)

    with mock.patch.object(Path, "read_bytes", failing_read):
        aa._install_session_key(cfg)  # the moment the old code clobbered the file

    assert key_file.read_bytes() == original, "a failed READ overwrote the key"

    # ...and once the file is readable again, the pre-existing cookie still works.
    aa._SESSION_KEYS.clear()
    assert read_session_user(cfg, token) == cfg.admin_username


def test_an_unreadable_key_is_not_cached_as_the_process_key(tmp_path):
    """Caching the ephemeral fallback would turn a transient error into a
    permanent one by the back door: every later request in this process would
    sign with the wrong key even after the file became readable."""
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    issue_session_token(cfg)
    aa._SESSION_KEYS.clear()
    real_read_bytes = Path.read_bytes

    def failing_read(self, *args, **kwargs):
        if self.name == aa.SESSION_KEY_FILENAME:
            raise OSError(13, "briefly locked")
        return real_read_bytes(self, *args, **kwargs)

    with mock.patch.object(Path, "read_bytes", failing_read):
        aa._install_session_key(cfg)

    assert str(Path(cfg.data_root) / aa.SESSION_KEY_FILENAME) not in aa._SESSION_KEYS


# The session key is 32 RANDOM BYTES. Every byte value is legal, so the two
# defects below were both invisible until a key happened to draw the wrong one —
# which is why they presented as a ~1-in-3 flake in this file rather than as a
# bug report. Each parametrization pins one hostile value in one position.
_WHITESPACE_BYTES = (0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20)


@pytest.mark.parametrize("byte", _WHITESPACE_BYTES)
@pytest.mark.parametrize("position", ["leading", "trailing"])
def test_a_key_edged_with_a_whitespace_byte_survives_a_restart(tmp_path, byte, position):
    """`.strip()` was applied to a 32-byte SECRET (5.6.3).

    Six byte values are whitespace, so a key that happened to begin or end with
    one came back short, failed the length check, and was regenerated and
    OVERWRITTEN — 4.7% of all keys, 1 - (250/256)^2. On those installs every
    admin session died on every restart, which is the exact property persisting
    this file exists to provide, and the only symptom was a debug line.

    Same disease as the read path this release began with: normalizing data that
    has to be handled byte for byte, in the least forgiving place there is.
    """
    body = os.urandom(31)
    key = bytes([byte]) + body if position == "leading" else body + bytes([byte])
    assert len(key) == 32

    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    aa._SESSION_KEYS.clear()
    with mock.patch.object(aa.secrets, "token_bytes", lambda n: key):
        token = issue_session_token(cfg)

    key_file = Path(cfg.data_root) / aa.SESSION_KEY_FILENAME
    assert key_file.read_bytes() == key, "the key was not persisted byte for byte"

    aa._SESSION_KEYS.clear()  # a restart
    assert read_session_user(cfg, token) == cfg.admin_username
    assert key_file.read_bytes() == key, "the restart silently replaced the key"


def test_a_key_containing_a_newline_byte_survives_a_restart(tmp_path):
    """os.open() defaults to TEXT MODE on Windows without O_BINARY (5.6.3).

    Every 0x0a in the key was written as 0x0d 0x0a, and a random 32-byte key
    contains a 0x0a 11.8% of the time — so the file held 33 bytes that were
    never the key, and the signing secret changed at the next start.

    This is the SAME defect 5.0.0 fixed for documents (_write_verbatim, where
    Path.write_text translated "\n" to os.linesep), still live in the one place
    the payload is a cryptographic secret. Runs on every platform on purpose:
    half of it is invisible on Linux, which is where production runs.
    """
    key = b"\x01\n\x02" + os.urandom(29)
    assert len(key) == 32 and b"\n" in key

    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    aa._SESSION_KEYS.clear()
    with mock.patch.object(aa.secrets, "token_bytes", lambda n: key):
        token = issue_session_token(cfg)

    key_file = Path(cfg.data_root) / aa.SESSION_KEY_FILENAME
    stored = key_file.read_bytes()
    assert stored == key, f"newline translation corrupted the key: {len(stored)} bytes"
    assert b"\r\n" not in stored

    aa._SESSION_KEYS.clear()  # a restart
    assert read_session_user(cfg, token) == cfg.admin_username


def test_an_all_newline_key_round_trips(tmp_path):
    """The pathological end of the same defect: 32 bytes that are all 0x0a
    became 64 bytes on disk. Vanishingly unlikely, and it isolates the
    translation with nothing else in the way."""
    key = b"\n" * 32
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    aa._SESSION_KEYS.clear()
    with mock.patch.object(aa.secrets, "token_bytes", lambda n: key):
        issue_session_token(cfg)
    assert (Path(cfg.data_root) / aa.SESSION_KEY_FILENAME).read_bytes() == key


def test_a_truncated_key_file_is_replaced(tmp_path):
    """The other half of the distinction: present but unusably short really is
    a file to rewrite — it cannot verify any existing cookie either way."""
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    issue_session_token(cfg)
    key_file = Path(cfg.data_root) / aa.SESSION_KEY_FILENAME
    key_file.write_bytes(b"short")
    aa._SESSION_KEYS.clear()

    key = aa._install_session_key(cfg)
    assert len(key) >= 32
    assert key_file.read_bytes() == key


def test_session_key_file_is_created_private(tmp_path):
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    issue_session_token(cfg)
    key_file = Path(cfg.data_root) / aa.SESSION_KEY_FILENAME
    assert key_file.is_file()
    assert len(key_file.read_bytes()) >= 32


def test_changing_the_password_still_invalidates_sessions(tmp_path):
    """The property the old derivation existed for, preserved."""
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    token = issue_session_token(cfg)
    rotated = _config(tmp_path, admin_password_sha256=hash_token("a new password"))
    assert read_session_user(rotated, token) is None


@pytest.mark.parametrize("bad", ["a.\u00e9", "\u00e9.a", "\u00e9", "..", ".", "a.b.c"])
def test_malformed_cookie_returns_none_never_raises(tmp_path, bad):
    """read_session_user's contract is "None on a bad token".

    Both operands of the signature compare came straight from the caller's
    cookie, which Starlette decodes latin-1, and hmac.compare_digest raises
    TypeError on non-ASCII str operands — so one curl with a non-ASCII cookie
    500'd every admin route, traceback in the log, instead of a clean redirect.
    """
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    assert read_session_user(cfg, bad) is None


def test_non_ascii_username_does_not_raise(tmp_path):
    """verify_login 500'd on a non-ASCII username, for the same compare_digest
    reason — and would have locked the admin out permanently had one been set,
    since set-admin-credentials.py accepts any input."""
    from cognita.admin_auth import verify_login

    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    assert verify_login(cfg, "\u00e4dmin", PW) is False
    assert verify_login(cfg, cfg.admin_username, PW) is True


# ------------------------------------------------ 5.1: logout, throttling, browse


async def test_logout_actually_revokes_the_session(tmp_path):
    """Logout was cosmetic. It called delete_cookie and nothing else — no
    session store, no issued-at floor — so a cookie captured beforehand kept
    working until exp (30 days by default) no matter how often the admin logged
    out. The only revocation lever was changing the password."""
    cfg = _config(tmp_path, admin_password_sha256=hash_token(PW))
    app = _app(cfg)
    async with await _client(app) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        assert r.status_code == 200
        stolen = c.cookies[SESSION_COOKIE]
        assert read_session_user(cfg, stolen) == "admin"

        assert (await c.post("/api/logout")).status_code == 200
        # the captured copy must no longer verify
        assert read_session_user(cfg, stolen) is None


async def test_login_is_throttled_after_repeated_failures(tmp_path):
    """No counter, delay or lockout existed on /api/login, over an unsalted
    single-round SHA-256 verifier — an unbounded online guessing loop."""
    import cognita.admin_api as admin_mod

    admin_mod._login_failures.clear()
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        for _ in range(admin_mod.LOGIN_MAX_FAILURES):
            r = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401
        r = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        # and the CORRECT password is refused too while locked out — otherwise
        # the lockout would not bound guessing at all
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        assert r.status_code == 429
    admin_mod._login_failures.clear()


async def test_successful_login_clears_the_failure_counter(tmp_path):
    import cognita.admin_api as admin_mod

    admin_mod._login_failures.clear()
    app = _app(_config(tmp_path, admin_password_sha256=hash_token(PW)))
    async with await _client(app) as c:
        for _ in range(admin_mod.LOGIN_MAX_FAILURES - 1):
            assert (await c.post("/api/login",
                                 json={"username": "admin", "password": "no"})).status_code == 401
        assert (await c.post("/api/login",
                             json={"username": "admin", "password": PW})).status_code == 200
        assert not admin_mod._login_failures
    admin_mod._login_failures.clear()


# ------------------------------------------------ 5.4: Host validation (rebinding)


def test_allowed_hosts_always_include_loopback(tmp_path):
    hosts = [h.lower() for h in allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=[]))]
    assert "localhost" in hosts and "127.0.0.1" in hosts


def test_allowed_hosts_include_this_machines_own_names(tmp_path):
    """Derived, not configured — an allow-list that omits the name the operator
    actually types locks them out of the admin UI with no route back but SSH,
    and that risk is why this check was not added earlier."""
    import socket

    hosts = [h.lower() for h in allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=[]))]
    assert socket.gethostname().lower() in hosts


def test_allowed_hosts_are_read_from_the_tls_certificate(tmp_path):
    """Derive allowed names and addresses from certificate SANs."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as _dt
    import ipaddress as _ip

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "host.example.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("host.example.test"),
            x509.DNSName("admin.example.test"),
            x509.IPAddress(_ip.ip_address("192.0.2.25")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "host.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    hosts = allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=[],
                                        admin_tls_certfile=str(certfile)))
    assert "host.example.test" in hosts
    assert "admin.example.test" in hosts
    assert "192.0.2.25" in hosts


def test_explicit_config_overrides_the_derivation(tmp_path):
    hosts = allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=["only.example"]))
    assert hosts == ["only.example"]


def test_star_disables_the_check(tmp_path):
    assert allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=["*"])) == ["*"]


async def test_a_rebound_host_is_refused_and_the_real_one_is_not(tmp_path):
    """The attack: a page the admin visits rebinds attacker.tld to this machine,
    and the browser then treats http://attacker.tld:8676/ as SAME-ORIGIN, reading
    responses from an authenticated admin session. SameSite=Lax does not help —
    after rebinding the request IS same-site."""
    cfg = _config(tmp_path, admin_password_sha256="", admin_allowed_hosts=["localhost"])
    app = _app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://attacker.tld") as c:
        assert (await c.get("/api/projects")).status_code == 400
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        assert (await c.get("/api/projects")).status_code == 200


def test_a_bad_certificate_path_does_not_break_startup(tmp_path):
    """Derivation must degrade to the local names, never raise — a surface that
    will not start is a worse outcome than one with a shorter allow-list."""
    hosts = allowed_admin_hosts(_config(tmp_path, admin_allowed_hosts=[],
                                        admin_tls_certfile=str(tmp_path / "nope.pem")))
    assert "localhost" in [h.lower() for h in hosts]
