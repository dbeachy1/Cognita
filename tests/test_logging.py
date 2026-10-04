"""Console/file log formatting + token redaction (see __main__._RedactingFormatter).

Bearer tokens ride in the /mcp/<token> URL path, so they land in uvicorn access
records. The formatter must scrub them from every line while leaving ordinary
text (e.g. hyphenated document filenames) untouched.
"""

import logging
import sys

import pytest

from cognita.__main__ import _RedactingFormatter, _RoutineHealthSuccessFilter, _redact

FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _access_record(path: str) -> logging.LogRecord:
    # Emitted exactly as uvicorn does (protocols/http/h11_impl.py): printf template.
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", "POST", path, "1.1", 200), None,
    )


def test_token_in_mcp_path_is_redacted():
    out = _RedactingFormatter(FMT).format(_access_record("/mcp/OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89"))
    assert "OwyQvlUamc" not in out
    assert "/mcp/<token>" in out
    # still a well-formed, timestamped access line
    assert "uvicorn.access:" in out
    assert '"POST /mcp/<token> HTTP/1.1" 200' in out


def test_plain_mcp_path_without_token_untouched():
    out = _RedactingFormatter(FMT).format(_access_record("/mcp"))
    assert "/mcp" in out and "<token>" not in out


def test_routine_health_filter_keeps_failures_and_real_requests():
    health_filter = _RoutineHealthSuccessFilter()
    success = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:43974", "GET", "/healthz", "1.1", 200), None,
    )
    assert not health_filter.filter(success)
    success.args = ("127.0.0.1:43974", "GET", "/healthz", "1.1", 503)
    assert health_filter.filter(success)
    success.args = ("203.0.113.10:43974", "GET", "/healthz", "1.1", 200)
    assert health_filter.filter(success)
    success.args = ("127.0.0.1:43974", "POST", "/mcp/v4", "1.1", 200)
    assert health_filter.filter(success)

    ready = logging.LogRecord(
        "httpx", logging.INFO, "x", 0,
        'HTTP Request: GET http://127.0.0.1:8778/_cognita/ready "HTTP/1.1 200 OK"',
        (), None,
    )
    assert not health_filter.filter(ready)
    ready.msg = 'HTTP Request: GET http://127.0.0.1:8778/_cognita/ready "HTTP/1.1 503 Service Unavailable"'
    assert health_filter.filter(ready)


def test_explicit_debug_token_record_is_not_redacted():
    """Debug Tokens Mode uses a distinct spelling so its one deliberate
    plaintext diagnostic survives while ordinary path/header logs stay safe."""
    token = "OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89"
    rec = logging.LogRecord(
        "cognita.gateway", logging.WARNING, "x", 0,
        "DEBUG TOKENS MODE project=KEI presented_token=%s", (token,), None,
    )
    out = _RedactingFormatter(FMT).format(rec)
    assert f"presented_token={token}" in out


def test_uvicorn_lifecycle_logger_quieted(tmp_path):
    """2.10.8: uvicorn's lifecycle logger is NAMED 'uvicorn.error' and its INFO
    chatter duplicated our banner (twice — one line per server). _setup_logging
    pins it to WARNING, and _make_server must pass log_level=None so uvicorn
    doesn't re-raise the level at serve time."""
    import inspect
    import logging

    from cognita.__main__ import _make_server, _setup_logging
    from cognita.config import CognitaConfig

    # log_dir=tmp_path: _setup_logging attaches a RotatingFileHandler to the ROOT
    # logger, so with the default log_dir this test wrote into the real
    # logs/cognita.log AND leaked every subsequent test's records there. Point it
    # at a temp dir and restore the root logger afterward so nothing leaks.
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        _setup_logging(CognitaConfig(log_dir=tmp_path))
        assert logging.getLogger("uvicorn.error").level == logging.WARNING
        # uvicorn.access must stay effective-INFO (inherits root) — request logging lives
        assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.INFO
        # the level pin only survives serve() if uvicorn is given no log_level
        assert "log_level=None" in inspect.getsource(_make_server)
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_hyphenated_filenames_not_mangled():
    # The redaction must be scoped to the token path, not any long a-z/-/_ run.
    rec = logging.LogRecord(
        "cognita.proxy", logging.INFO, "x", 0,
        "backed up project-Research-Notes-and-Index.md", (), None,
    )
    out = _RedactingFormatter(FMT).format(rec)
    assert "project-Research-Notes-and-Index.md" in out
    assert "<token>" not in out


# --- colorized console formatter (4.1.2) ---

ESC = "\033["


def test_color_formatter_still_redacts_tokens():
    """The color path must scrub tokens exactly like the plain one — a colorized
    line is still a logged line. This is the security invariant, non-negotiable."""
    from cognita.__main__ import _ColorFormatter

    out = _ColorFormatter().format(_access_record("/mcp/OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89"))
    assert "OwyQvlUamc" not in out
    assert "/mcp/<token>" in out


def test_color_formatter_emits_ansi_by_level():
    from cognita.__main__ import _ColorFormatter

    def line(level):
        rec = logging.LogRecord("cognita", level, "x", 0, "hello", (), None)
        return _ColorFormatter().format(rec)

    info, warn, err = line(logging.INFO), line(logging.WARNING), line(logging.ERROR)
    assert ESC in info and "\033[1;32m" in info      # bright green INFO level
    assert "\033[1;33m" in warn                       # bright yellow WARNING
    assert "\033[1;31m" in err                        # bright red ERROR
    # warnings/errors tint the message body too; info leaves it uncolored
    assert warn.count("\033[1;33m") >= 2
    assert info.rstrip().endswith("hello")           # message not wrapped in color


def _fmt(name, level, msg):
    from cognita.__main__ import _ColorFormatter

    return _ColorFormatter().format(
        logging.LogRecord(name, level, "x", 0, msg, (), None)
    )


YELLOW = "\033[1;33m"


def test_gpu_info_lines_are_tinted_yellow():
    """6.0.1: GPU work is spottable by eye at INFO, not just when it WARNs."""
    out = _fmt("cognita.embed", logging.INFO,
               "embed.plan project=p walk=w planned=gpu decision=gpu")
    assert f"{YELLOW}embed.plan" in out


def test_gpu_logger_is_tinted_even_when_text_never_says_gpu():
    """Relayed worker output (§14.6) carries no GPU token of its own."""
    out = _fmt("cognita.gpu", logging.INFO, "card2 loaded in 31.4s")
    assert f"{YELLOW}card2 loaded" in out


def test_vram_and_provider_tokens_match():
    for msg in ("peak_vram=3.62GB released=True",
                "MIGraphX ENV Override Variables Set",
                "embed.done  cpu_fallback_chunks=512"):
        assert YELLOW in _fmt("cognita.retrieval", logging.INFO, msg), msg


def test_cpu_walk_is_not_tinted():
    """The discrimination that matters: `cognita.embed` carries BOTH paths, so a
    CPU walk must stay plain or the yellow means nothing."""
    out = _fmt("cognita.embed", logging.INFO,
               "embed.plan project=p walk=w planned=cpu decision=cpu chunks=12")
    assert YELLOW not in out
    assert out.rstrip().endswith("chunks=12")


def test_gpu_error_stays_red():
    """A GPU ERROR must not be downgraded to the yellow tint."""
    out = _fmt("cognita.gpu", logging.ERROR, "canary FAILED on card1")
    assert "\033[1;31m" in out
    assert YELLOW not in out


def test_color_formatter_renders_exceptions():
    from cognita.__main__ import _ColorFormatter

    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("cognita", logging.ERROR, "x", 0, "failed", (), sys.exc_info())
    out = _ColorFormatter().format(rec)
    assert "Traceback" in out and "ValueError: boom" in out


def test_want_color_modes(monkeypatch):
    from cognita.__main__ import _want_color

    monkeypatch.delenv("NO_COLOR", raising=False)

    class TTY:
        def isatty(self):
            return True

    class NotTTY:
        def isatty(self):
            return False

    assert _want_color("always", NotTTY()) is True
    assert _want_color("never", TTY()) is False
    assert _want_color("auto", TTY()) is True
    assert _want_color("auto", NotTTY()) is False


def test_want_color_respects_no_color(monkeypatch):
    from cognita.__main__ import _want_color

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    assert _want_color("always", TTY()) is False  # NO_COLOR wins even over "always"


def _formatters(tmp_path, mode):
    """Run _setup_logging with a color mode; return (console_fmt, file_fmt),
    restoring the root logger afterward."""
    from logging.handlers import RotatingFileHandler

    from cognita.__main__ import _setup_logging
    from cognita.config import CognitaConfig

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        _setup_logging(CognitaConfig(log_dir=tmp_path, log_color=mode))
        stream_h = [h for h in root.handlers if type(h) is logging.StreamHandler]
        file_h = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        return stream_h[0].formatter, (file_h[0].formatter if file_h else None)
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_always_colors_both_console_and_file(tmp_path, monkeypatch):
    """log_color=always colors the log you actually tail (cognita.log), not just
    the console — the whole point."""
    from cognita.__main__ import _ColorFormatter

    monkeypatch.delenv("NO_COLOR", raising=False)

    console_fmt, file_fmt = _formatters(tmp_path, "always")
    assert isinstance(console_fmt, _ColorFormatter)
    assert isinstance(file_fmt, _ColorFormatter)


def test_auto_leaves_file_plain(tmp_path):
    """auto: a file is never a TTY, so cognita.log stays plain (grep-safe default)."""
    from cognita.__main__ import _ColorFormatter, _RedactingFormatter

    _console_fmt, file_fmt = _formatters(tmp_path, "auto")
    assert type(file_fmt) is _RedactingFormatter
    assert not isinstance(file_fmt, _ColorFormatter)


# ------------------------------------------------- 5.1: the spellings that leaked


@pytest.mark.parametrize(
    "line",
    [
        "POST /mcp/{t} HTTP/1.1 200",
        # Case-sensitivity: a hand-typed connector URL.
        "POST /MCP/{t} HTTP/1.1 404",
        # An empty segment where the token was expected — the ordinary result of
        # a client whose base URL ends in "/". Both of these are 404s at the
        # router, so gateway._log_unmatched redacts ITS line; the uvicorn access
        # line is emitted separately and only this rule stands between the token
        # and logs/cognita.log, which is rotated and kept x5.
        "POST /mcp//{t} HTTP/1.1 404",
        "POST /mcp///{t} HTTP/1.1 404",
        "GET /mcp/{t}/ HTTP/1.1 200",
        # The header form. DESIGN-5.0 section 1 documents it as an equally
        # supported access path; nothing logs headers today, but that is a fact
        # about today's code rather than an invariant.
        "Authorization: Bearer {t}",
        "authorization: bearer {t}",
    ],
)
def test_every_token_spelling_is_redacted(line):
    token = "pxJXQ7QvVqhLFA_BeLogTmOtJP6lJbA_1zI-UcnHWq4"
    out = _redact(line.format(t=token))
    assert token not in out, out
    assert "<token>" in out
