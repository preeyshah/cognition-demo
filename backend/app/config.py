import secrets
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://refunds:refunds@localhost:5432/refunds"
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 8

    # Comma-separated origins the SSO flow may redirect back to.
    allowed_redirect_origins: str = (
        "http://localhost:8080,http://localhost:5173"
    )
    cors_origins: str = "*"

    history_provider_url: str = "http://localhost:9000"
    history_provider_api_key: str = "demo-provider-key"
    history_provider_timeout_s: float = 3.0

    seed_on_startup: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _random_secret_if_blank(cls, v: str) -> str:
        # Blank means "not configured": use an ephemeral random key rather than a known default.
        return v or secrets.token_urlsafe(48)

    @property
    def redirect_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_redirect_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
