"""RDS rotates its managed DB password under running ECS tasks (request #2, trace tr_f50783d09ff64958a7ee23d124da88af)."""

import psycopg
import pytest

from app.config import Settings
from app.db import RotatingPassword, connect_with_rotating_password, make_engine, password_source

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-abc"
AUTH_FAILED = 'connection failed: FATAL:  password authentication failed for user "refunds_app"'


class FakeDialect:
    loaded_dbapi = psycopg

    def __init__(self, server_password: str, error: str = AUTH_FAILED):
        self.server_password = server_password
        self.error = error
        self.attempts: list[str] = []

    def connect(self, *cargs, **cparams):
        self.attempts.append(cparams["password"])
        if cparams["password"] != self.server_password:
            raise psycopg.OperationalError(self.error)
        return object()


def _connect(creds: RotatingPassword, dialect: FakeDialect):
    return connect_with_rotating_password(creds)(dialect, None, (), {"user": "refunds_app", "password": "from-url"})


def test_reconnects_with_rotated_password():
    fetches = []
    creds = RotatingPassword("injected-at-task-start", fetch=lambda: fetches.append(1) or "rotated")
    dialect = FakeDialect(server_password="rotated")

    assert _connect(creds, dialect) is not None
    assert dialect.attempts == ["injected-at-task-start", "rotated"]

    assert _connect(creds, dialect) is not None  # later connections use the new password straight away
    assert dialect.attempts[-1] == "rotated" and len(fetches) == 1


def test_wrong_password_still_fails_and_refresh_is_throttled():
    now = [0.0]
    fetches = []
    creds = RotatingPassword("bad", fetch=lambda: fetches.append(1) or "bad", clock=lambda: now[0])
    dialect = FakeDialect(server_password="right")

    for _ in range(3):
        with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
            _connect(creds, dialect)
    assert len(fetches) == 1  # no Secrets Manager call per connection attempt
    now[0] = 31.0
    with pytest.raises(psycopg.OperationalError):
        _connect(creds, dialect)
    assert len(fetches) == 2


def test_other_connection_errors_are_not_retried():
    fetches = []
    creds = RotatingPassword("pw", fetch=lambda: fetches.append(1) or "other")
    dialect = FakeDialect(server_password="never", error="connection refused")
    with pytest.raises(psycopg.OperationalError, match="refused"):
        _connect(creds, dialect)
    assert dialect.attempts == ["pw"] and not fetches


def test_concurrent_failure_reuses_already_refreshed_password():
    fetches = []
    creds = RotatingPassword("old", fetch=lambda: fetches.append(1) or "new")
    assert creds.refresh("old") == "new"
    assert creds.refresh("old") == "new"  # a second connection that also saw "old" rejected
    assert len(fetches) == 1


def test_rds_settings_enable_rotation_aware_engine(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(db_host="db.internal", db_user="refunds_app", db_password="stale", db_secret_arn=SECRET_ARN, _env_file=None)
    creds = password_source(s)
    assert creds is not None and creds.get() == "stale"
    engine = make_engine(s.database_url, creds)
    assert len(engine.dialect.dispatch.do_connect) == 1


def test_no_secret_arn_keeps_static_password(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert password_source(Settings(db_host="db.internal", db_user="u", db_password="p", _env_file=None)) is None
    assert password_source(Settings(database_url="sqlite://", db_secret_arn=SECRET_ARN, _env_file=None)) is None
