"""HTTP middleware: request id, timing, message-size guard happens at schema level."""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import bind_request_context, clear_request_context, get_logger

log = get_logger("api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        bind_request_context(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            log.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                elapsed_ms=elapsed_ms,
            )
            clear_request_context()
        response.headers["X-Request-ID"] = request_id
        return response
