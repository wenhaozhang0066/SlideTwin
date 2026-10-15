"""Map core errors to HTTP responses with one envelope: {"error": {code, message, hint, ...}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from slidetwin.core.errors import SlideTwinError

log = logging.getLogger("slidetwin.api")

STATUS = {
    "INVALID_PARAMETER": 400,
    "MAG_NOT_PRESET": 400,
    "MAG_UNAVAILABLE": 400,
    "OUTSIDE_VIEW": 400,
    "NO_CURRENT_VIEW": 409,
    "ACTION_NOT_ALLOWED": 403,
    "BUDGET_EXHAUSTED": 409,
    "SESSION_CLOSED": 409,
    "SESSION_NOT_FOUND": 404,
    "TOO_MANY_SESSIONS": 503,
    "IMAGE_NOT_FOUND": 404,
    "SLIDE_NOT_FOUND": 404,
    "SLIDE_METADATA": 422,
    "SLIDE_READ_ERROR": 500,
    "SLIDE_HASH_MISMATCH": 500,
    "CONFIG_ERROR": 500,
    "REPORT_INVALID": 422,
    "RULE_VIOLATION": 422,
    "REPLAY_ERROR": 422,
    "INTERNAL": 500,
}
RETRY_AFTER_S = "60"


def error_response(
    status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


async def _slidetwin_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, SlideTwinError)
    status = STATUS.get(exc.code, 500)
    if status >= 500:
        log.error("%s on %s %s: %s", exc.code, request.method, request.url.path, exc.message)
    headers = {"Retry-After": RETRY_AFTER_S} if exc.code == "TOO_MANY_SESSIONS" else None
    return error_response(status, exc.to_dict(), headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
        for e in exc.errors()[:10]
    ]
    first = details[0] if details else {"loc": [], "msg": "invalid request"}
    where = ".".join(first["loc"][1:]) or "body"
    return error_response(
        422,
        {
            "code": "INVALID_REQUEST",
            "message": f"Invalid request: {where}: {first['msg']}.",
            "hint": "See the OpenAPI schema at /docs.",
            "details": details,
        },
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 405:
        allowed = (headers or {}).get("Allow", "")
        return error_response(
            405,
            {
                "code": "METHOD_NOT_ALLOWED",
                "message": f"{request.method} is not allowed on {request.url.path}; "
                f"use {allowed or 'the method listed in /docs'}.",
            },
            headers,
        )
    if exc.status_code == 404:
        return error_response(
            404,
            {
                "code": "NOT_FOUND",
                "message": f"There is no endpoint {request.url.path}.",
                "hint": "See the API at /docs.",
            },
        )
    if exc.status_code == 400:  # FastAPI could not decode the body (e.g. not UTF-8 JSON)
        return error_response(
            422,
            {
                "code": "INVALID_REQUEST",
                "message": "The request body is not valid UTF-8 JSON.",
                "hint": "Send a JSON object with content-type application/json.",
            },
        )
    return error_response(
        exc.status_code, {"code": "HTTP_ERROR", "message": str(exc.detail)}, headers
    )


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(
        500,
        {"code": "INTERNAL", "message": "Internal error; the request was not completed."},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SlideTwinError, _slidetwin_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _internal_error)


class BodySizeLimit:
    """Reject request bodies larger than `max_bytes` with 413.

    The body (at most `max_bytes`) is buffered before the app sees it, so oversized bodies are
    refused whether or not they declare a Content-Length.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await self._reject(send)
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # client disconnected before sending the body
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        response = error_response(
            413,
            {
                "code": "PAYLOAD_TOO_LARGE",
                "message": f"Request body is larger than {self.max_bytes} bytes.",
            },
        )
        await send({"type": "http.response.start", "status": 413, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})
