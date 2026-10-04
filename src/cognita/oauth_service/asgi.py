"""ASGI application factory for the Cognita OAuth child."""

from __future__ import annotations

from typing import Any

_application: Any = None


def create_application(ctx):
    from django.core.asgi import get_asgi_application

    from .policy import ResourcePolicy, set_policy
    from .settings import configure_django
    from .views import set_context

    configure_django(ctx.config, ctx.key, ctx.store_path)
    set_context(ctx)
    set_policy(ResourcePolicy(ctx.config, ctx.registry))
    global _application
    _application = get_asgi_application()
    return _application


def get_application(ctx):
    return create_application(ctx)
