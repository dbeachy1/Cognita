"""Cognita — self-hosted, multi-tenant RAG gateway over MCP."""

# 13.0 §4: the version is not spelled here either. `release_identity` is the
# single authority and a leaf module, so importing the package never costs more
# than it did when this was a literal.
from .release_identity import APPLICATION_VERSION as __version__

__all__ = ["__version__"]
