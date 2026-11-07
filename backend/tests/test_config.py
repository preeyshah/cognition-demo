import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ENVIRONMENT", "JWT_SECRET", "SEED_ON_STARTUP", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_to_dev():
    s = Settings(_env_file=None)
    assert s.environment == "dev"
    assert s.mock_sso_enabled
    assert s.seed_on_startup


def test_prod_requires_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="prod", _env_file=None)


def test_prod_disables_seed_by_default():
    s = Settings(environment="prod", jwt_secret="x" * 48, _env_file=None)
    assert not s.seed_on_startup
    assert not s.mock_sso_enabled


def test_prod_explicit_seed_respected():
    s = Settings(environment="prod", jwt_secret="x" * 48, seed_on_startup=True, _env_file=None)
    assert s.seed_on_startup


def test_invalid_environment_rejected():
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        Settings(environment="staging", _env_file=None)
