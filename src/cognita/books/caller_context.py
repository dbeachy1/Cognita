"""Request-local authenticated authority for remote book imports."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..auth_policy import AuthPrincipal
from ..registry import Project
from .models import WorkspaceAudioSource
from .sources import StagedAudioSource


WriteAdmission = Callable[[], tuple[str, str] | None]


class WorkspaceStager(Protocol):
    async def __call__(
        self,
        source: WorkspaceAudioSource,
        *,
        staging_root: Path,
        max_bytes: int,
        reserve_bytes: int,
    ) -> StagedAudioSource: ...


@dataclass(frozen=True, slots=True)
class BookCallerContext:
    """Actual gateway-authenticated caller and the scoped source capabilities."""

    principal: AuthPrincipal
    project: Project
    connector_id: str
    check_write_admission: WriteAdmission
    stage_workspace: WorkspaceStager


CURRENT_BOOK_CALLER: ContextVar[BookCallerContext | None] = ContextVar(
    "cognita_book_caller", default=None
)
