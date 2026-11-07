"""Request tracing: every request gets a trace id that is stamped on logs, audit rows,
refund events and the request_traces table, and returned to the caller as X-Trace-Id."""

import json
import logging
import re
import sys
import time
import traceback
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

TRACE_HEADER = "X-Trace-Id"
_INCOMING_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
# A mutable holder: endpoints/deps run in copied contexts, so they mutate it rather than re-set the var.
_actor: ContextVar[dict | None] = ContextVar("trace_actor", default=None)
log = logging.getLogger("refunds")


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex}"


def current_trace_id() -> str | None:
    return _trace_id.get()


def set_actor(user_id: int, email: str) -> None:
    holder = _actor.get()
    if holder is not None:
        holder.update(id=user_id, email=email)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": current_trace_id(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our structured request log


def _fingerprint(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    app_frames = [f for f in frames if "/app/" in f.filename] or frames
    last = app_frames[-1] if app_frames else None
    where = f"{last.filename.rsplit('/app/', 1)[-1]}:{last.name}" if last else "unknown"
    return f"{type(exc).__name__}@{where}"


class TracingMiddleware(BaseHTTPMiddleware):
    """Assigns the trace id, records one request_traces row per request and turns
    unhandled exceptions into a 500 that carries the trace id (and can open a Devin fix)."""

    def __init__(self, app, record, on_error):
        super().__init__(app)
        self._record = record
        self._on_error = on_error

    async def dispatch(self, request: Request, call_next):
        # Always server-generated so a caller can't merge their actions into someone else's trace.
        trace_id = new_trace_id()
        incoming = request.headers.get(TRACE_HEADER) or request.headers.get("X-Request-ID")
        client_ref = incoming if incoming and _INCOMING_OK.match(incoming) else None
        token = _trace_id.set(trace_id)
        actor_token = _actor.set({})
        start = time.perf_counter()
        error = None
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - last-resort handler
                error = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "fingerprint": _fingerprint(exc),
                    "stack": "".join(traceback.format_exception(exc))[-8000:],
                }
                log.exception("unhandled error", extra={"fields": {"path": request.url.path}})
                response = JSONResponse({"detail": "Internal server error", "trace_id": trace_id}, status_code=500)
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            response.headers[TRACE_HEADER] = trace_id
            actor = _actor.get()
            entry = {
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "query": ((f"client_ref={client_ref}&" if client_ref else "") + request.url.query) or None,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "actor_id": actor.get("id"),
                "actor_email": actor.get("email"),
                "error": error,
            }
            if request.url.path != "/health":
                log.info("request", extra={"fields": {k: v for k, v in entry.items() if k != "error"}})
                self._record(entry)
            if error:
                try:
                    self._on_error(entry)
                except Exception:  # noqa: BLE001 - e.g. DB down; the traced 500 must still reach the caller
                    log.exception("failed to open error ticket")
            return response
        finally:
            _actor.reset(actor_token)
            _trace_id.reset(token)
