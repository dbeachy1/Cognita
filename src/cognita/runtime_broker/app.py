"""FastAPI entry point for the private Cognita runtime broker."""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..release_identity import APPLICATION_VERSION
from .journal import RequestJournal
from .protocol import (
    MAX_TRANSFER_FRAME_BYTES,
    ErrorCode,
    HealthResponse,
    RpcFailure,
    RpcRequest,
    TransferManifest,
)
from .sdk_adapter import RuntimeAdapter, UnavailableAdapter
from .service import BrokerService
from .state import RuntimeStateStore
from .transfers import TransferStore

log = logging.getLogger(__name__)
MAX_RPC_BODY_BYTES = 256 * 1024
INTERNAL_BEARER_ENV = "COGNITA_RUNTIME_BROKER_SECRET"
INTERNAL_BEARER_FILE_ENV = "COGNITA_INTERNAL_BEARER_FILE"


def _strict_json(body: bytes) -> Any:
    """Decode one JSON value while rejecting duplicate keys and constants."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = value
        return output

    def constant(value: str) -> None:
        raise ValueError(f"JSON constant {value} is not allowed")

    return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


async def _bounded_body(request: Request, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes, without buffering an oversized request."""

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _failure(
    code: ErrorCode,
    message: str,
    *,
    request_id: UUID | None = None,
    retryable: bool = False,
) -> JSONResponse:
    payload = RpcFailure(request_id=request_id, code=code, retryable=retryable, message=message)
    return JSONResponse(status_code=400, content=payload.model_dump(mode="json", exclude_none=True))


def _configured_secret(explicit: str | None) -> str:
    """Load the internal bearer without placing it in process arguments or logs."""

    if explicit is not None:
        value = explicit
    elif os.environ.get(INTERNAL_BEARER_FILE_ENV):
        path = Path(os.environ[INTERNAL_BEARER_FILE_ENV])
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("runtime broker secret file is unavailable") from exc
    else:
        value = os.environ.get(INTERNAL_BEARER_ENV, "")
    if not value:
        raise ValueError("runtime broker secret must be configured")
    if len(value.encode("ascii", errors="ignore")) < 32 or not value.isascii():
        raise ValueError("runtime broker secret must contain at least 256 bits")
    if any(
        character.isspace() or ord(character) < 0x21 or ord(character) > 0x7E for character in value
    ):
        raise ValueError("runtime broker secret must be printable ASCII without whitespace")
    return value


def create_app(
    *,
    secret: str | None = None,
    adapter: RuntimeAdapter | None = None,
    journal: RequestJournal | None = None,
    state_store: RuntimeStateStore | None = None,
    startup_probe: bool = False,
) -> FastAPI:
    """Build the private app without importing Cognita auth or Knowledge code."""

    # Refuse to start a network-capable app without the mounted secret.
    configured_secret = _configured_secret(secret)

    service = BrokerService(adapter or UnavailableAdapter(), journal, state_store=state_store)
    copy_from_host = getattr(service.adapter, "copy_from_host", None)
    copy_to_host = getattr(service.adapter, "copy_to_host", None)

    async def import_file(workspace_id: UUID, relative_path: str, host_path: str) -> None:
        if not callable(copy_from_host):
            raise RuntimeError("runtime transfer import is unavailable")
        await copy_from_host(workspace_id, host_path, f"/workspace/{relative_path}")

    async def export_file(workspace_id: UUID, relative_path: str, host_path: str) -> None:
        if not callable(copy_to_host):
            raise RuntimeError("runtime transfer export is unavailable")
        await copy_to_host(workspace_id, f"/workspace/{relative_path}", host_path)

    transfers = TransferStore(
        copy_from_host=import_file if callable(copy_from_host) else None,
        copy_to_host=export_file if callable(copy_to_host) else None,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if startup_probe:
            try:
                await service.startup()
            except (OSError, RuntimeError, ValueError):
                log.warning("workspace runtime SDK readiness probe failed", extra={"stage": "startup"})
        try:
            yield
        finally:
            await service.shutdown()
            transfers.close()
            service.journal.close()
            service.state_store.close()

    app = FastAPI(
        title="Cognita Workspace Runtime Broker",
        # 13.0 §4: the broker reports the application version from the same
        # authority the gateway does, so "which build is this broker?" cannot
        # be answered differently by the two halves of one deployment.
        version=APPLICATION_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.broker_service = service
    app.state.transfer_store = transfers

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default 422 includes field locations and input values.  The
        # broker protocol intentionally exposes neither to its private caller.
        return _failure(ErrorCode.INVALID_REQUEST, "request does not match the broker schema")

    async def authenticate(authorization: str | None) -> None:
        expected = "Bearer " + configured_secret
        if authorization is None or not hmac.compare_digest(authorization, expected):
            # Never echo the supplied value or distinguish missing from invalid.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid broker credentials",
            )

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> dict[str, Any]:
        return service.health()

    @app.post("/v1/rpc")
    async def rpc(request: Request, authorization: str | None = Header(default=None)) -> Response:
        await authenticate(authorization)
        body = await _bounded_body(request, MAX_RPC_BODY_BYTES)
        if body is None:
            return _failure(ErrorCode.INVALID_REQUEST, "request body exceeds broker limit")
        try:
            parsed = RpcRequest.model_validate(_strict_json(body))
        except (ValidationError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return _failure(ErrorCode.INVALID_REQUEST, "request does not match the broker schema")
        result = await service.handle(parsed)
        # Both success and normalized failure envelopes are JSON 200/4xx based
        # on protocol contents; callers must inspect ``code`` for failures.
        return JSONResponse(status_code=200, content=result.model_dump(mode="json", exclude_none=True))

    @app.post("/v1/transfers")
    async def admit_transfer(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Response:
        await authenticate(authorization)
        body = await _bounded_body(request, MAX_RPC_BODY_BYTES)
        if body is None:
            return _failure(ErrorCode.INVALID_REQUEST, "manifest exceeds broker limit")
        try:
            manifest = TransferManifest.model_validate(_strict_json(body))
            state = await transfers.admit(manifest)
        except (ValidationError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return _failure(ErrorCode.INVALID_REQUEST, "transfer manifest is invalid")
        except (OSError, RuntimeError):
            return _failure(
                ErrorCode.RUNTIME_FAILURE,
                "workspace transfer staging failed",
                retryable=True,
            )
        return JSONResponse(status_code=200, content=state.model_dump(mode="json"))

    @app.get("/v1/transfers/{transfer_id}")
    async def transfer_state(
        transfer_id: UUID, authorization: str | None = Header(default=None)
    ) -> Response:
        await authenticate(authorization)
        try:
            state = transfers.state(transfer_id)
        except KeyError:
            return _failure(ErrorCode.NOT_FOUND, "transfer was not found")
        return JSONResponse(status_code=200, content=state.model_dump(mode="json"))

    @app.put("/v1/transfers/{transfer_id}/content")
    async def put_content(
        transfer_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_transfer_path: str | None = Header(default=None),
        x_frame_offset: int | None = Header(default=None),
        x_frame_sha256: str | None = Header(default=None),
    ) -> Response:
        await authenticate(authorization)
        if x_transfer_path is None or x_frame_offset is None or x_frame_sha256 is None:
            return _failure(ErrorCode.INVALID_REQUEST, "transfer frame headers are required")
        content = await _bounded_body(request, MAX_TRANSFER_FRAME_BYTES)
        if content is None:
            return _failure(ErrorCode.INVALID_REQUEST, "transfer frame exceeds broker limit")
        try:
            state = transfers.put_frame(
                transfer_id, x_transfer_path, x_frame_offset, content, x_frame_sha256
            )
        except KeyError:
            return _failure(ErrorCode.NOT_FOUND, "transfer was not found")
        except ValueError:
            return _failure(ErrorCode.INVALID_REQUEST, "transfer frame is invalid")
        return JSONResponse(status_code=200, content=state.model_dump(mode="json"))

    @app.get("/v1/transfers/{transfer_id}/content")
    async def get_content(
        transfer_id: UUID,
        authorization: str | None = Header(default=None),
        path: str | None = None,
        offset: int = 0,
        length: int | None = MAX_TRANSFER_FRAME_BYTES,
    ) -> Response:
        await authenticate(authorization)
        if (
            path is None
            or offset < 0
            or (length is not None and (length < 0 or length > MAX_TRANSFER_FRAME_BYTES))
        ):
            return _failure(ErrorCode.INVALID_REQUEST, "content range is invalid")
        try:
            content = transfers.content(transfer_id, path, offset, length)
        except KeyError:
            return _failure(ErrorCode.NOT_FOUND, "transfer was not found")
        except ValueError:
            return _failure(ErrorCode.INVALID_REQUEST, "content range is invalid")
        return StreamingResponse(iter([content]), media_type="application/octet-stream")

    @app.post("/v1/transfers/{transfer_id}/commit")
    async def commit_transfer(
        transfer_id: UUID, authorization: str | None = Header(default=None)
    ) -> Response:
        await authenticate(authorization)
        try:
            state = await transfers.commit(transfer_id)
        except KeyError:
            return _failure(ErrorCode.NOT_FOUND, "transfer was not found")
        except ValueError:
            return _failure(ErrorCode.INVALID_REQUEST, "transfer is not ready to commit")
        except (OSError, RuntimeError):
            return _failure(
                ErrorCode.RUNTIME_FAILURE,
                "workspace transfer commit failed",
                retryable=True,
            )
        return JSONResponse(status_code=200, content=state.model_dump(mode="json"))

    @app.post("/v1/transfers/{transfer_id}/abort")
    async def abort_transfer(
        transfer_id: UUID, authorization: str | None = Header(default=None)
    ) -> Response:
        await authenticate(authorization)
        try:
            state = transfers.abort(transfer_id)
        except KeyError:
            return _failure(ErrorCode.NOT_FOUND, "transfer was not found")
        except ValueError:
            return _failure(ErrorCode.INVALID_REQUEST, "transfer cannot be aborted")
        return JSONResponse(status_code=200, content=state.model_dump(mode="json"))

    return app
