from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import get_settings
from .db import Base, SessionLocal, engine
from .routers import admin, auth, dashboard, refunds
from .seed import seed


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
    yield


app = FastAPI(title="Refunds Console API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)
for r in (auth.router, refunds.router, dashboard.router, admin.router):
    app.include_router(r)


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    return {"ok": True}
