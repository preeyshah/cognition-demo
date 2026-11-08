import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings, origin_pattern_regex
from .db import Base, SessionLocal, engine
from .models import RequestTrace
from .payouts import PayoutWorker
from .remediation import record_error, start_worker
from .routers import admin, auth, compliance, dashboard, monitoring, payouts, refunds
from .seed import seed
from .tracing import TRACE_HEADER, TracingMiddleware, configure_logging

log = logging.getLogger("refunds")


def record_trace(entry: dict) -> None:
    try:
        with SessionLocal() as db:
            db.add(RequestTrace(**entry))
            db.commit()
    except Exception:  # noqa: BLE001 - tracing must never break a request
        log.exception("failed to persist request trace")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def init_db() -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        sql = (Path(__file__).parent / "sql" / "analytics.sql").read_text()
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
    if get_settings().seed_on_startup:
        with SessionLocal() as db:
            seed(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    s = get_settings()
    stop_remediation = start_worker() if s.remediation_worker_enabled else None
    worker = PayoutWorker(SessionLocal, s.payout_worker_interval_s) if s.payout_worker_enabled else None
    if worker:
        worker.start()
    yield
    if worker:
        worker.stop()
    if stop_remediation:
        stop_remediation()


configure_logging(get_settings().log_level)
_cors_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
app = FastAPI(title="Refunds Console API", version="0.1.0", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in _cors_origins if "*" not in o],
    allow_origin_regex=[rx for o in _cors_origins if (rx := origin_pattern_regex(o))],
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["Authorization", "Content-Type", TRACE_HEADER],
    expose_headers=[TRACE_HEADER],
)
app.add_middleware(TracingMiddleware, record=record_trace, on_error=record_error)
for r in (auth.router, refunds.router, dashboard.router, admin.router, monitoring.router, payouts.router,
          payouts.webhooks, compliance.router):
    app.include_router(r)
if get_settings().mock_sso_enabled:
    app.include_router(auth.sso_router)


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    return {"ok": True}
