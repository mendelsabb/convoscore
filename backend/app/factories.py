"""How the API, worker and ingestor build their dependencies.

One place, so all three processes agree on which queue and which provider they are using.
"""

from __future__ import annotations

from app.config import Settings
from app.logging import get_logger
from app.queue import SqsQueue, build_sqs_client
from app.scoring.provider import FakeProvider, LLMProvider, OpenAIProvider
from app.storage import ObjectStore, build_s3_client

log = get_logger(__name__)


def _aws_secret(settings: Settings) -> str | None:
    return (
        settings.aws_secret_access_key.get_secret_value()
        if settings.aws_secret_access_key
        else None
    )


def build_queue(settings: Settings) -> SqsQueue:
    client = build_sqs_client(
        endpoint_url=settings.aws_endpoint_url,
        region=settings.aws_region,
        access_key_id=settings.aws_access_key_id,
        secret_access_key=_aws_secret(settings),
    )
    return SqsQueue(client, settings.sqs_queue_name)


def build_storage(settings: Settings) -> ObjectStore:
    client = build_s3_client(
        endpoint_url=settings.aws_endpoint_url,
        region=settings.aws_region,
        access_key_id=settings.aws_access_key_id,
        secret_access_key=_aws_secret(settings),
    )
    return ObjectStore(client, settings.s3_bucket, settings.s3_prefix)


def build_provider(settings: Settings) -> LLMProvider:
    """Pick the scoring backend.

    ``LLM_PROVIDER=fake`` runs the entire stack deterministically and at zero cost, which is what
    the automated tests use. It is also a genuine fallback for a demo without a key: the pipeline,
    the metrics and the failure injection all behave identically.
    """
    if settings.llm_provider == "fake":
        log.info("llm_provider_selected", extra={"provider": "fake"})
        return FakeProvider()

    if not settings.openai_key_configured:
        raise RuntimeError(
            "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
            "Provide it in .env, or set LLM_PROVIDER=fake to run without OpenAI."
        )

    assert settings.openai_api_key is not None
    log.info(
        "llm_provider_selected",
        extra={"provider": "openai", "model": settings.openai_model},
    )
    return OpenAIProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        temperature=settings.openai_temperature,
        base_url=settings.openai_base_url,
    )
