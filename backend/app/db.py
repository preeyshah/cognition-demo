import json
import logging
import threading
import time
from collections.abc import Callable, Iterator

from sqlalchemy import create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings, get_settings

log = logging.getLogger(__name__)

# 28P01 invalid_password, 28000 invalid_authorization_specification
_AUTH_FAILED_SQLSTATES = {"28P01", "28000"}


class Base(DeclarativeBase):
    pass


class RotatingPassword:
    """DB password that is re-read from its source when the server rejects it.

    RDS rotates its managed master password in Secrets Manager, but ECS only injects
    DB_PASSWORD when a task starts, so a long-running task would otherwise keep the
    stale value and fail every new connection.
    """

    def __init__(self, initial: str, fetch: Callable[[], str], min_refresh_interval_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self._password = initial
        self._fetch = fetch
        self._min_interval = min_refresh_interval_s
        self._clock = clock
        self._last_refresh: float | None = None
        self._lock = threading.Lock()

    def get(self) -> str:
        return self._password

    def refresh(self, rejected: str) -> str | None:
        """Return a password worth retrying with after `rejected` failed, or None."""
        with self._lock:
            if self._password != rejected:
                return self._password  # another connection already picked up the new one
            now = self._clock()
            if self._last_refresh is not None and now - self._last_refresh < self._min_interval:
                return None
            try:
                fresh = self._fetch()
            except Exception:
                log.exception("could not read the database password from Secrets Manager")
                return None
            self._last_refresh = now
            if fresh == rejected:
                return None
            self._password = fresh
            return fresh


def secrets_manager_password(secret_arn: str) -> Callable[[], str]:
    """Read the `password` field of an RDS-managed Secrets Manager secret."""
    client = None

    def fetch() -> str:
        nonlocal client
        if client is None:
            import boto3

            client = boto3.client("secretsmanager", region_name=secret_arn.split(":")[3])
        return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])["password"]

    return fetch


def _is_auth_failure(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) in _AUTH_FAILED_SQLSTATES or "password authentication failed" in str(exc)


def connect_with_rotating_password(password: RotatingPassword):
    def do_connect(dialect, conn_rec, cargs, cparams):
        current = password.get()
        cparams["password"] = current
        try:
            return dialect.connect(*cargs, **cparams)
        except dialect.loaded_dbapi.OperationalError as exc:
            if not _is_auth_failure(exc):
                raise
            fresh = password.refresh(current)
            if fresh is None:
                raise
            log.warning("database password rejected; retrying with the rotated password from Secrets Manager")
            cparams["password"] = fresh
            return dialect.connect(*cargs, **cparams)

    return do_connect


def password_source(settings: Settings) -> RotatingPassword | None:
    if not settings.db_secret_arn or settings.database_url.startswith("sqlite"):
        return None
    initial = make_url(settings.database_url).password or ""
    return RotatingPassword(initial, secrets_manager_password(settings.db_secret_arn))


def make_engine(url: str, password: RotatingPassword | None = None):
    if url.startswith("sqlite"):
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    engine = create_engine(url, pool_pre_ping=True)
    if password is not None:
        event.listen(engine, "do_connect", connect_with_rotating_password(password))
    return engine


engine = make_engine(get_settings().database_url, password_source(get_settings()))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
