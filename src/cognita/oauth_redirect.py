"""The one rule for where Cognita's OAuth flow may send a browser back to.

16.1.5 (2026-10-15): any MCP client may register, through DCR or a client
metadata document. Before this, redirect hosts had to be on a configured list
(Claude, ChatGPT, OpenAI, loopback), which locked out every other OAuth client;
TypingMind was the first to hit it. The password-gated consent page is the real
gate, and it names the callback host so the approver can see where access goes.

What remains is the URI's shape: HTTPS to any host, or plain HTTP only to this
machine's loopback (RFC 8252 native-app callbacks). No userinfo and no
fragment. The same rule checks registered URIs and the proxy's outgoing
Location (which carries the code in its query), so it sets no length cap;
registration bodies are already bounded. This module has no Django dependency,
so the parent proxy and the OAuth child process share it.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def is_ipv6_literal(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


def redirect_uri_safe(uri: object) -> bool:
    if not isinstance(uri, str) or not uri or "\x00" in uri:
        return False
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.fragment or parsed.username or parsed.password or not host:
        return False
    # The consent page shows this host to the approver. A Unicode lookalike
    # (Cyrillic "а" in "clаude.ai") or a percent-encoded one would read as a
    # trusted site while the browser goes to the attacker's punycode domain.
    # Real IDN callbacks still work in their xn-- form.
    if not host.isascii() or "%" in host:
        return False
    # The host also lands in the consent page's CSP header and text, so allow
    # only hostname characters or an IPv6 literal, nothing a header could parse.
    if not (_HOSTNAME.fullmatch(host) or is_ipv6_literal(host)):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    scheme = parsed.scheme.lower()
    if scheme == "http":
        return host.lower() in LOOPBACK_HOSTS
    return scheme == "https"
