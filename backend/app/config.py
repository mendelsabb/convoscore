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
    # Must comfortably exceed the LLM timeout plus database work, otherwise SQS would redeliver a
    # message that is still being processed.
    visibility_timeout_seconds: int = Field(default=90, ge=10, le=900)
    worker_wait_time_seconds: int = Field(default=20, ge=0, le=20)
    # Retry backoff: min(max, base * 2^attempt) seconds, plus jitter, applied by extending the
    # message's visibility timeout so the retry survives this worker dying.
    retry_base_backoff_seconds: int = Field(default=5, ge=1, le=60)
    retry_max_backoff_seconds: int = Field(default=60, ge=1, le=900)

    # --- AWS / LocalStack ---------------------------------------------------------
    # Empty endpoint means real AWS; locally this points at LocalStack.
    aws_endpoint_url: str | None = None
    aws_region: str = "us-east-1"
    # Unset in production: boto3 then uses the pod's IAM role rather than static credentials.
    aws_access_key_id: str | None = None
    aws_secret_access_key: SecretStr | None = None
    sqs_queue_name: str = "convoscore-scoring"
    s3_bucket: str = "convoscore-conversations"
    # Only this prefix is read, and the ingestor's IAM policy is scoped to it.
    s3_prefix: str = "incoming/"
    ingest_poll_interval_seconds: int = Field(default=10, ge=1, le=3600)

    # --- LLM ----------------------------------------------------------------------
    llm_provider: Literal["openai", "fake"] = "openai"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str | None = None
    openai_timeout_seconds: float = Field(default=30.0, gt=0)
    # None sends no temperature at all, which reasoning models require.
    openai_temperature: float | None = 0.0

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
        """Configuration that is safe to expose over the API and in logs.

        Deliberately excludes every credential. Whether a key is *configured* is useful to an
        operator; its value never is.
        """
        return {
            "environment": self.environment,
            "component": self.component,
            "demo_mode": self.demo_mode,
            "max_attempts": self.max_attempts,
            "llm_provider": self.llm_provider,
            "model": self.openai_model if self.llm_provider == "openai" else "fake-model",
            "queue": self.sqs_queue_name,
            "bucket": self.s3_bucket,
            "prefix": self.s3_prefix,
        }

    @property
    def openai_key_configured(self) -> bool:
        key = self.openai_api_key.get_secret_value() if self.openai_api_key else ""
        return bool(key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
