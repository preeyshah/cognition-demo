import secrets
from functools import lru_cache
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEMO_WEBHOOK_SECRET = "whsec_demo_only"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "dev" enables the mock SSO IdP and demo seeding; "prod" disables both and hardens defaults.
    environment: str = "dev"
    database_url: str = "postgresql+psycopg://refunds:refunds@localhost:5432/refunds"
    jwt_secret: str = ""  # unset => random per process (tokens die on restart)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 8

    # Comma-separated origins the SSO flow may redirect back to.
    allowed_redirect_origins: str = (
        "http://localhost:8080,http://localhost:5173"
    )
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    log_level: str = "INFO"
    trusted_proxy_hops: int = 0

    # On AWS the DB password is injected from Secrets Manager; DATABASE_URL is assembled from parts.
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "refunds"
    db_user: str = ""
    db_password: str = ""
    db_sslmode: str = "require"

    # Self-healing: hand errors / support requests to Devin, which opens a PR on this repo.
    devin_api_url: str = "https://api.devin.ai"
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_repo: str = "preeyshah/cognition-demo"
    devin_base_branch: str = "dev"  # auto-fix PRs land on dev; promote via dev → main
    devin_max_acu: int = 10  # hard cost cap per auto-remediation session
    auto_remediate_errors: bool = True
    remediation_cooldown_minutes: int = 60

    history_provider_url: str = "http://localhost:9000"
    history_provider_api_key: str = "demo-provider-key"
    history_provider_timeout_s: float = 3.0

    # Payments provider (PayCore) used to actually move the money once a refund is approved.
    payments_provider_url: str = "http://localhost:9000"
    payments_provider_api_key: str = "demo-provider-key"
    payments_webhook_secret: str = DEMO_WEBHOOK_SECRET  # shared with the provider; set from a secret store
    payments_webhook_tolerance_s: int = 300  # reject stale/replayed webhook signatures
    payments_timeout_s: float = 5.0
    payments_sandbox_controls: bool = True  # admin UI can inject provider failures (sandbox only)
    payout_max_attempts: int = 5
    payout_retry_base_s: float = 2.0
    payout_retry_max_s: float = 60.0
    payout_worker_enabled: bool = True
    payout_worker_interval_s: float = 2.0

    # Dev-only mock second factor on the SSO picker; ignored when ENVIRONMENT=prod.
    mock_mfa_code: str = "123456"

    # A burst of denied actions (guardrail/policy violations, probing) auto-opens a
    # support ticket that the remediation loop hands to Devin. 0 disables the alert.
    guardrail_alert_threshold: int = 50
    guardrail_alert_window_minutes: int = 10

    seed_on_startup: bool = True

    @model_validator(mode="before")
    @classmethod
    def _prod_requires_jwt_secret(cls, data):
        if isinstance(data, dict) and str(data.get("environment") or "dev").lower() == "prod":
            if not data.get("jwt_secret"):
                raise ValueError("JWT_SECRET must be set when ENVIRONMENT=prod")
        return data

    @model_validator(mode="after")
    def _environment_defaults(self):
        self.environment = self.environment.strip().lower()
        if self.environment not in ("dev", "prod"):
            raise ValueError("ENVIRONMENT must be 'dev' or 'prod'")
        # Demo data (users, customers, refunds) must never appear in prod unless explicitly requested.
        if self.environment == "prod" and "seed_on_startup" not in self.model_fields_set:
            self.seed_on_startup = False
        # Provider failure injection is a sandbox demo tool; never expose it in prod by default.
        if self.environment == "prod" and self.payments_webhook_secret == DEMO_WEBHOOK_SECRET:
            raise ValueError("PAYMENTS_WEBHOOK_SECRET must be set when ENVIRONMENT=prod")
        if self.environment == "prod" and "payments_sandbox_controls" not in self.model_fields_set:
            self.payments_sandbox_controls = False
        return self

    @property
    def mock_sso_enabled(self) -> bool:
        return self.environment == "dev"

    @model_validator(mode="after")
    def _database_url_from_parts(self):
        explicit_url = "database_url" in self.model_fields_set and self.database_url
        if self.db_host and not explicit_url:
            self.database_url = (
                f"postgresql+psycopg://{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode={self.db_sslmode}"
            )
        return self

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
