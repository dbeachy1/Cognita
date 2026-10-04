"""Django OAuth Toolkit host used by Cognita 8.0's loopback OAuth child."""
from __future__ import annotations

__all__ = ["ServiceContext", "bootstrap_service"]


def __getattr__(name):
    if name in {"ServiceContext", "bootstrap_service"}:
        from .bootstrap import ServiceContext, bootstrap_service
        return {"ServiceContext": ServiceContext, "bootstrap_service": bootstrap_service}[name]
    raise AttributeError(name)
