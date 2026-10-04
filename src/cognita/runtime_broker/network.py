"""Guest network policy and trusted-side Brave Search isolation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from .validation import is_forbidden_ip

NetworkMode = Literal["off", "allowlist", "unrestricted_public"]
NETWORK_SCHEMES = ("http", "https")
_DOMAIN = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


class BraveRateLimitError(RuntimeError):
    pass


class BraveQuotaError(RuntimeError):
    pass


def brave_http_search(*, query: str, count: int, api_key: str) -> dict[str, Any]:
    """Call Brave's trusted-side Web Search endpoint without exposing its key."""
    import httpx

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            )
    except httpx.TimeoutException as exc:
        raise TimeoutError from exc
    if response.status_code in {401, 403}:
        raise PermissionError
    if response.status_code == 429:
        raise BraveRateLimitError
    if response.status_code == 402:
        raise BraveQuotaError
    response.raise_for_status()
    payload = response.json()
    web = payload.get("web", {}) if isinstance(payload, dict) else {}
    return {"results": web.get("results", []) if isinstance(web, dict) else []}


@dataclass(frozen=True)
class NetworkRule:
    domain: str
    ports: tuple[int, ...] = (80, 443)
    protocols: tuple[str, ...] = NETWORK_SCHEMES
    suffix: bool = False

    def __post_init__(self) -> None:
        domain = self.domain.lower().rstrip(".")
        if not _DOMAIN.fullmatch(domain):
            raise ValueError("network rules require a valid DNS domain")
        if not self.ports or any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in self.ports):
            raise ValueError("network rule port is invalid")
        if not self.protocols or any(not isinstance(protocol, str) or protocol not in {"http", "https"} for protocol in self.protocols):
            raise ValueError("network rule protocol is invalid")
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "ports", tuple(sorted(set(self.ports))))
        object.__setattr__(self, "protocols", tuple(sorted(set(self.protocols))))

    @property
    def availability(self) -> str:
        """Return whether the SDK can enforce this domain/port rule safely."""
        return "available" if self.protocols == NETWORK_SCHEMES else "unavailable"

    @property
    def availability_reason(self) -> str | None:
        """Explain why a legacy scheme-specific rule remains fail-closed."""
        if self.availability == "available":
            return None
        return "Edit this rule to apply it to both HTTP and HTTPS."

    def matches(self, hostname: str, port: int, protocol: str) -> bool:
        host = hostname.lower().rstrip(".")
        domain_match = host == self.domain or (self.suffix and host.endswith("." + self.domain))
        return domain_match and port in self.ports and protocol.lower() in self.protocols

    def to_mapping(self) -> dict[str, Any]:
        """Return the structured wire representation used by the broker.

        Rules stay objects all the way through validation and the SDK seam;
        converting them to display strings loses ports, protocols, and suffix
        semantics and was the source of the old ``[object Object]`` bug.
        """
        return {
            "domain": self.domain,
            "ports": list(self.ports),
            "protocols": list(self.protocols),
            "suffix": self.suffix,
        }


@dataclass(frozen=True)
class NetworkPolicy:
    mode: NetworkMode = "off"
    rules: tuple[NetworkRule, ...] = ()
    explicit_confirmation: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"off", "allowlist", "unrestricted_public"}:
            raise ValueError("unsupported network mode")
        if self.mode == "off" and self.rules:
            raise ValueError("network-off policy cannot contain allowlist rules")
        if self.mode == "unrestricted_public" and not self.explicit_confirmation:
            raise ValueError("unrestricted public networking requires explicit confirmation")
        if len(self.rules) > 256:
            raise ValueError("network allowlist is too large")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "NetworkPolicy":
        value = value or {}
        unknown = set(value) - {"mode", "rules", "explicit_confirmation"}
        if unknown:
            raise ValueError("unknown network policy field")
        mode = value.get("mode", "off")
        raw_rules = value.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("network rules must be a list")
        rules: list[NetworkRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict) or set(raw) - {"domain", "ports", "protocols", "suffix"}:
                raise ValueError("network rule is invalid")
            ports = raw.get("ports", [80, 443])
            protocols = raw.get("protocols", list(NETWORK_SCHEMES))
            if not isinstance(ports, list) or not isinstance(protocols, list):
                raise ValueError("network rule ports/protocols are invalid")
            suffix = raw.get("suffix", False)
            if not isinstance(suffix, bool) or not isinstance(raw.get("domain", ""), str):
                raise ValueError("network rule domain/suffix is invalid")
            rules.append(NetworkRule(raw.get("domain", ""), tuple(ports), tuple(protocols), suffix))
        confirmation = value.get("explicit_confirmation", False)
        if not isinstance(confirmation, bool):
            raise ValueError("network confirmation must be boolean")
        return cls(mode=mode, rules=tuple(rules),
                   explicit_confirmation=confirmation)

    def to_mapping(self) -> dict[str, Any]:
        """Serialize a normalized policy without collapsing its rule objects."""
        return {
            "mode": self.mode,
            "rules": [rule.to_mapping() for rule in self.rules],
            "explicit_confirmation": self.explicit_confirmation,
        }

    def allows(self, hostname: str, port: int, protocol: str,
               resolved_addresses: list[str] | None = None) -> bool:
        """Apply the fail-closed destination checks used before guest egress."""
        addresses = resolved_addresses or []
        if not addresses or any(is_forbidden_ip(address) for address in addresses):
            return False
        if self.mode == "off":
            return False
        if self.mode == "unrestricted_public":
            return any(not is_forbidden_ip(address) for address in addresses) \
                and protocol.lower() in {"http", "https"} and 1 <= port <= 65535
        return any(rule.matches(hostname, port, protocol) for rule in self.rules)


class BraveSearchService:
    """Trusted-side Brave adapter; its API key is never guest-visible."""

    def __init__(self, client: Callable[..., Any] | None = None) -> None:
        self._client = client
        self._enabled = False
        self._api_key: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._api_key) and self._client is not None

    def configure(self, api_key: str, *, enabled: bool) -> None:
        if not isinstance(api_key, str) or len(api_key.encode("utf-8")) < 16 or any(character.isspace() for character in api_key):
            raise ValueError("Brave API key is invalid")
        self._api_key = api_key
        self._enabled = bool(enabled)

    def disable(self) -> None:
        self._enabled = False

    def search(self, query: str, *, count: int = 5) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "error", "reason": "network_denied"}
        if not isinstance(query, str) or not query.strip() or len(query.encode()) > 4096:
            return {"status": "error", "reason": "invalid_arguments"}
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            return {"status": "error", "reason": "invalid_arguments"}
        try:
            # The key is passed only to the trusted client callback.  Do not
            # include it in any exception or returned value.
            payload = self._client(query=query, count=count, api_key=self._api_key)
        except TimeoutError:
            return {"status": "error", "reason": "timeout"}
        except PermissionError:
            return {"status": "error", "reason": "authentication"}
        except BraveRateLimitError:
            return {"status": "error", "reason": "rate_limit"}
        except BraveQuotaError:
            return {"status": "error", "reason": "quota"}
        except Exception:  # noqa: BLE001 - provider errors are intentionally bounded
            return {"status": "error", "reason": "network_failure"}
        results = payload.get("results", []) if isinstance(payload, dict) else []
        bounded = []
        for item in results[:count]:
            if not isinstance(item, dict):
                continue
            bounded.append({key: item[key] for key in ("title", "url", "snippet")
                            if isinstance(item.get(key), str) and len(item[key]) <= 4096})
        return {"status": "success", "results": bounded}
