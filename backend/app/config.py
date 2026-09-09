"""Application configuration.

Every value comes from the environment. Secrets are held in ``SecretStr`` so that an accidental
log or traceback prints ``**********`` instead of the credential.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Component = Literal["api", "worker", "ingestor", "migrate", "test"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # --- identity -----------------------------------------------------------------
    environment: str = "local"
    component: Component = "api"

    # --- logging ------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # --- database -----------------------------------------------------------------
    # DATABASE_URL wins when set; otherwise it is assembled from the parts below so that the
    # password can come from its own Kubernetes Secret key.
    database_url: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "convoscore"
    postgres_user: str = "convoscore"
    postgres_password: SecretStr = SecretStr("convoscore")
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 10_000

    # --- job processing -----------------------------------------------------------
    max_attempts: int = Field(default=3, ge=1, le=10)
    visibility_timeout_seconds: int = Field(default=90, ge=10, le=900)

    # --- demo ---------------------------------------------------------------------
    demo_mode: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy URL for psycopg 3."""
        if self.database_url:
            return self.database_url
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def safe_summary(self) -> dict[str, object]:
        """Configuration that is safe to expose over the API and in logs."""
        return {
            "environment": self.environment,
            "component": self.component,
            "demo_mode": self.demo_mode,
            "max_attempts": self.max_attempts,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
