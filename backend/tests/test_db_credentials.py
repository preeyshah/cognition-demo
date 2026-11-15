"""RDS rotates the master password in Secrets Manager; ECS only injects DB_PASSWORD at task start.

Request #1 / trace tr_195f08a1a539461988e3476c9669f188: after a rotation, every new pooled
connection from a long-running task failed with "password authentication failed".
"""

import json
import sys
import types

import psycopg
import pytest

from app.config import Settings
from app.db import DbCredentials, credentials_from_settings, make_engine, secrets_manager_fetcher

URL = "postgresql+psycopg://refunds_app:old-pw@10.40.21.215:5432/refunds?sslmode=require"
AUTH_ERROR = (
    'connection failed: connection to server at "10.40.21.215", port 5432 failed: '
    'FATAL:  password authentication failed for user "refunds_app"'
)


class FakeServer:
    """Stands in for psycopg.connect against an RDS instance whose password was rotated."""

    def __init__(self, password="new-pw"):
        self.password = password
        self.attempts: list[str] = []

    def connect(self, *args, **kwargs):
        self.attempts.append(kwargs.get("password"))
        if kwargs.get("password") != self.password:
            raise psycopg.OperationalError(AUTH_ERROR)
        return object()


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(psycopg, "connect", fake.connect)
    return fake


def open_raw(engine):
    return engine.pool._creator(None)


def test_stale_env_password_without_secret_store_fails(server):
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        open_raw(make_engine(URL))


def test_rotated_password_is_reloaded_from_secret_store(server):
    fetches = []

    def fetch():
        fetches.append(1)
        return "refunds_app", "new-pw"

    creds = DbCredentials(fetch, user="refunds_app", password="old-pw")
    engine = make_engine(URL, credentials=creds)

    assert open_raw(engine) is not None
    assert server.attempts == ["old-pw", "new-pw"]
    assert len(fetches) == 1

    # Later connections reuse the refreshed password without another Secrets Manager call.
    open_raw(engine)
    assert server.attempts[-1] == "new-pw"
    assert len(fetches) == 1


def test_still_wrong_after_refresh_raises(server):
    creds = DbCredentials(lambda: ("refunds_app", "also-wrong"), user="refunds_app", password="old-pw")
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        open_raw(make_engine(URL, credentials=creds))
    assert server.attempts == ["old-pw", "also-wrong"]


def test_non_auth_errors_do_not_refetch(monkeypatch):
    def unreachable(*a, **k):
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(psycopg, "connect", unreachable)
    fetches = []
    creds = DbCredentials(lambda: fetches.append(1) or ("u", "p"), user="u", password="p")
    with pytest.raises(psycopg.OperationalError, match="timeout"):
        open_raw(make_engine(URL, credentials=creds))
    assert fetches == []


def test_credentials_fetched_lazily_when_not_injected(server):
    creds = DbCredentials(lambda: ("refunds_app", "new-pw"))
    assert open_raw(make_engine(URL, credentials=creds)) is not None
    assert server.attempts == ["new-pw"]


def test_secrets_manager_fetcher_reads_rds_secret(monkeypatch):
    calls = {}

    class Client:
        def get_secret_value(self, SecretId):
            calls["secret"] = SecretId
            return {"SecretString": json.dumps({"username": "refunds_app", "password": "new-pw"})}

    def client(service, region_name=None):
        calls["service"], calls["region"] = service, region_name
        return Client()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-abc-123"
    assert secrets_manager_fetcher(arn)() == ("refunds_app", "new-pw")
    assert calls == {"secret": arn, "service": "secretsmanager", "region": "us-east-1"}


ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-abc-123"


def test_secret_arn_applies_to_url_built_from_parts(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(db_host="db", db_user="refunds_app", db_password="old-pw", db_secret_arn=ARN, _env_file=None)
    assert credentials_from_settings(s).get() == ("refunds_app", "old-pw")


def test_secret_arn_ignored_for_explicit_database_url():
    s = Settings(
        database_url="postgresql+psycopg://other:pw@host/db", db_host="db", db_secret_arn=ARN, _env_file=None
    )
    assert credentials_from_settings(s) is None
