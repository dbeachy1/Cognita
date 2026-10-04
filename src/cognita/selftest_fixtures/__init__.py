"""Repository-owned synthetic fixtures for the persistent Cognita self-test.

The fixture package is deliberately separate from the self-test plan.  It owns
only the small, deterministic files that deployment may copy into the explicit
``Self-Test`` documents directory and the bounded provisioner used to do so.
"""

from .provision import (
    FIXTURE_MANIFEST_NAME,
    PROJECT_NAME,
    FixtureProvisionError,
    FixtureSpec,
    ProvisionResult,
    load_manifest,
    provision_self_test,
)

__all__ = [
    "FIXTURE_MANIFEST_NAME",
    "PROJECT_NAME",
    "FixtureProvisionError",
    "FixtureSpec",
    "ProvisionResult",
    "load_manifest",
    "provision_self_test",
]
