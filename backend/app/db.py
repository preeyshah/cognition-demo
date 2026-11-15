import json
import logging
import threading
from collections.abc import Callable, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings, get_settings

log = logging.getLogger(__name__)

Creds = tuple[str, str]


class Base(DeclarativeBase):
    pass


class DbCredentials:
    """Current DB user/password, re-read from the secret store when the server rejects them.

    RDS rotates its managed master password in Secrets Manager, but ECS only injects
    ``DB_PASSWORD`` at task start, so a long-running task must be able to pick up the new one.
    """

    def __init__(self, fetch: Callable[[], Creds], user: str = "", password: str = ""):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._creds: Creds | None = (user, password) if user and password else None

    def get(self) -> Creds:
        with self._lock:
            if self._creds is None:
                self._creds = self._fetch()
            return self._creds

    def refresh(self, rejected: Creds) -> Creds:
        with self._lock:
            if self._creds in (None, rejected):  # another thread may already have refreshed
                self._creds = self._fetch()
            return self._creds


def secrets_manager_fetcher(secret_arn: str) -> Callable[[], Creds]:
    """Reads an RDS-managed master user secret (JSON with ``username``/``password``)."""

    def fetch() -> Creds:
        import boto3

        region = secret_arn.split(":")[3]
        raw = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_arn)
        data = json.loads(raw["SecretString"])
        return data["username"], data["password"]

    return fetch


def _is_auth_failure(exc: Exception) -> bool:
    return "password authentication failed" in str(exc)


def _use_rotating_credentials(engine, credentials: DbCredentials) -> None:
    @event.listens_for(engine, "do_connect")
    def _connect(dialect, conn_rec, cargs, cparams):
        creds = credentials.get()
        cparams["user"], cparams["password"] = creds
        try:
            return dialect.connect(*cargs, **cparams)
        except dialect.loaded_dbapi.OperationalError as exc:
            if not _is_auth_failure(exc):
                raise
            log.warning("database rejected the DB password; reloading it from Secrets Manager")
            cparams["user"], cparams["password"] = credentials.refresh(creds)
            return dialect.connect(*cargs, **cparams)


def make_engine(url: str, credentials: DbCredentials | None = None):
    if url.startswith("sqlite"):
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    engine = create_engine(url, pool_pre_ping=True)
    if credentials is not None:
        _use_rotating_credentials(engine, credentials)
    return engine


def credentials_from_settings(settings: Settings) -> DbCredentials | None:
    if not settings.db_secret_arn or not settings.db_url_from_parts:
        return None
    return DbCredentials(
        secrets_manager_fetcher(settings.db_secret_arn), settings.db_user, settings.db_password
    )


engine = make_engine(get_settings().database_url, credentials_from_settings(get_settings()))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
