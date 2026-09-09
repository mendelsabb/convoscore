"""Storage ingestion against a real PostgreSQL and a mocked S3.

The property that matters most here: polling sees the same objects repeatedly, so ingestion must
be idempotent. Without that, every poll would create another job and another OpenAI charge for a
conversation that was already scored.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingestor import MAX_OBJECT_BYTES, Ingestor
from app.models import ErrorType, IngestStatus, JobSource, JobStatus
from app.queue import FailingPublisher, InMemoryPublisher
from app.repository import get_job, ingest_counts, list_conversations
from app.schemas import ConversationInput
from app.storage import ObjectStore, build_s3_client
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]

BUCKET = "convoscore-conversations-test"
PREFIX = "incoming/"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def store(s3) -> ObjectStore:
    return ObjectStore(s3, BUCKET, PREFIX)


@pytest.fixture
def publisher() -> InMemoryPublisher:
    return InMemoryPublisher()


@pytest.fixture
def ingestor_settings() -> Settings:
    return Settings(component="ingestor", s3_bucket=BUCKET, s3_prefix=PREFIX, max_attempts=3)


@pytest.fixture
def make_ingestor(ingestor_settings: Settings, store: ObjectStore, session: Session):
    def _make(publisher) -> Ingestor:
        return Ingestor(ingestor_settings, store, publisher, session_factory=lambda: session)

    return _make


@pytest.fixture
def ingestor(make_ingestor, publisher: InMemoryPublisher) -> Ingestor:
    return make_ingestor(publisher)


def put(s3, key: str, payload: object, prefix: str = PREFIX) -> None:
    body = payload if isinstance(payload, str | bytes) else json.dumps(payload)
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}{key}", Body=body)


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_object_becomes_a_job_on_the_same_pipeline(
    ingestor: Ingestor, s3, session: Session, publisher: InMemoryPublisher
) -> None:
    put(s3, "a.json", sample_conversation())

    result = ingestor.poll_once()

    assert (result.discovered, result.ingested) == (1, 1)
    rows, total = list_conversations(session)
    assert total == 1
    job = rows[0]
    assert job.source == JobSource.S3.value
    assert job.source_ref == f"s3://{BUCKET}/{PREFIX}a.json"
    assert job.status == JobStatus.PENDING.value
    assert job.enqueued_at is not None
    # Same queue as the API path: one pipeline, two doors.
    assert publisher.published == [job.id]


def test_ingested_object_is_recorded(ingestor: Ingestor, s3, session: Session) -> None:
    put(s3, "a.json", sample_conversation())
    ingestor.poll_once()
    assert ingest_counts(session) == {IngestStatus.INGESTED.value: 1}


def test_several_objects_are_ingested_in_one_pass(ingestor: Ingestor, s3, session: Session) -> None:
    for name in ("a.json", "b.json", "c.json"):
        put(s3, name, sample_conversation())

    result = ingestor.poll_once()

    assert (result.discovered, result.ingested) == (3, 3)
    _, total = list_conversations(session)
    assert total == 3


def test_empty_bucket_is_not_an_error(ingestor: Ingestor) -> None:
    result = ingestor.poll_once()
    assert (result.discovered, result.ingested) == (0, 0)


# --------------------------------------------------------------------------------------
# Duplicate protection
# --------------------------------------------------------------------------------------


def test_repeated_polling_does_not_create_duplicate_jobs(
    ingestor: Ingestor, s3, session: Session, publisher: InMemoryPublisher
) -> None:
    """The whole reason the ingested_objects table exists."""
    put(s3, "a.json", sample_conversation())

    first = ingestor.poll_once()
    second = ingestor.poll_once()
    third = ingestor.poll_once()

    assert first.ingested == 1
    assert (second.ingested, second.skipped_duplicate) == (0, 1)
    assert (third.ingested, third.skipped_duplicate) == (0, 1)

    _, total = list_conversations(session)
    assert total == 1
    assert len(publisher.published) == 1


def test_reuploading_the_same_content_is_still_a_duplicate(
    ingestor: Ingestor, s3, session: Session
) -> None:
    """Identical bytes at the same key produce the same etag, so nothing new has happened."""
    put(s3, "a.json", sample_conversation())
    ingestor.poll_once()

    put(s3, "a.json", sample_conversation())
    result = ingestor.poll_once()

    assert result.skipped_duplicate == 1
    _, total = list_conversations(session)
    assert total == 1


def test_reuploading_changed_content_creates_a_new_job(
    ingestor: Ingestor, s3, session: Session
) -> None:
    """A corrected export at the same key is new work, and the etag is what tells us."""
    put(s3, "a.json", sample_conversation())
    ingestor.poll_once()

    corrected = sample_conversation()
    corrected["messages"].append({"role": "customer", "content": "Adding a correction here."})
    put(s3, "a.json", corrected)
    result = ingestor.poll_once()

    assert result.ingested == 1
    _, total = list_conversations(session)
    assert total == 2


# --------------------------------------------------------------------------------------
# Bad input
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload", "reason"),
    [
        ("not-json.json", "this is not json", "unparsable"),
        ("array.json", [{"role": "customer", "content": "hi"}], "not an object"),
        ("no-messages.json", {"metadata": {"channel": "email"}}, "missing messages"),
        ("agent-only.json", {"messages": [{"role": "agent", "content": "hello?"}]}, "no customer"),
        ("bad-role.json", {"messages": [{"role": "bot", "content": "hi"}]}, "unknown role"),
    ],
)
def test_invalid_objects_are_rejected_and_recorded(
    ingestor: Ingestor, s3, session: Session, name: str, payload: object, reason: str
) -> None:
    put(s3, name, payload)

    result = ingestor.poll_once()

    assert result.invalid == 1, reason
    assert ingest_counts(session) == {IngestStatus.INVALID.value: 1}
    _, total = list_conversations(session)
    assert total == 0, "an invalid object must not create a job"


def test_an_invalid_object_is_not_re_read_on_every_poll(
    ingestor: Ingestor, s3, session: Session
) -> None:
    """Recording the rejection is what stops a bad file being reprocessed forever."""
    put(s3, "bad.json", "not json")

    assert ingestor.poll_once().invalid == 1
    assert ingestor.poll_once().skipped_duplicate == 1
    assert ingest_counts(session) == {IngestStatus.INVALID.value: 1}


def test_oversized_object_is_rejected(ingestor: Ingestor, s3, session: Session) -> None:
    put(s3, "huge.json", "x" * (MAX_OBJECT_BYTES + 1))
    assert ingestor.poll_once().invalid == 1


def test_one_bad_object_does_not_stop_the_others(
    ingestor: Ingestor, s3, session: Session
) -> None:
    put(s3, "a-bad.json", "not json")
    put(s3, "b-good.json", sample_conversation())

    result = ingestor.poll_once()

    assert (result.ingested, result.invalid) == (1, 1)
    _, total = list_conversations(session)
    assert total == 1


# --------------------------------------------------------------------------------------
# Scope and infrastructure failures
# --------------------------------------------------------------------------------------


def test_only_the_incoming_prefix_is_read(ingestor: Ingestor, s3, session: Session) -> None:
    """Processed or unrelated objects elsewhere in the bucket are none of our business."""
    put(s3, "a.json", sample_conversation())
    put(s3, "b.json", sample_conversation(), prefix="archive/")

    result = ingestor.poll_once()

    assert (result.discovered, result.ingested) == (1, 1)


def test_directory_placeholders_are_ignored(ingestor: Ingestor, s3) -> None:
    s3.put_object(Bucket=BUCKET, Key=PREFIX, Body=b"")
    assert ingestor.poll_once().discovered == 0


def test_enqueue_failure_marks_the_job_failed(make_ingestor, s3, session: Session) -> None:
    """Same dual-write gap as the API, surfaced the same way rather than left pending."""
    put(s3, "a.json", sample_conversation())

    result = make_ingestor(FailingPublisher("queue down")).poll_once()

    assert result.failed == 1
    rows, _ = list_conversations(session)
    assert rows[0].status == JobStatus.FAILED.value
    assert rows[0].last_error_type == ErrorType.ENQUEUE_FAILED.value


def test_unreadable_object_is_retried_on_the_next_poll(
    ingestor: Ingestor, s3, store: ObjectStore, session: Session
) -> None:
    """A read failure is usually transient, so the object must not be marked handled."""
    put(s3, "a.json", sample_conversation())

    from app.storage import StorageUnavailableError

    original = store.read_object
    store.read_object = lambda key: (_ for _ in ()).throw(StorageUnavailableError("network"))
    assert ingestor.poll_once().failed == 1
    assert ingest_counts(session) == {}, "a transient read failure must not be recorded"

    store.read_object = original
    assert ingestor.poll_once().ingested == 1


def test_missing_bucket_raises_rather_than_silently_doing_nothing(
    ingestor_settings: Settings, session: Session
) -> None:
    from app.storage import StorageUnavailableError

    with mock_aws():
        client = build_s3_client(endpoint_url=None, region="us-east-1")
        store = ObjectStore(client, "bucket-that-does-not-exist", PREFIX)
        ingestor = Ingestor(
            ingestor_settings, store, InMemoryPublisher(), session_factory=lambda: session
        )
        with pytest.raises(StorageUnavailableError):
            ingestor.poll_once()


def test_run_forever_exits_promptly_once_stopped(ingestor: Ingestor) -> None:
    ingestor.request_stop()
    ingestor.run_forever()


# --------------------------------------------------------------------------------------
# Demo fixtures
# --------------------------------------------------------------------------------------


FIXTURE_ROOT = Path(__file__).resolve().parents[2].parent / "demo" / "fixtures"


def _fixtures(subdirectory: str) -> list[Path]:
    return sorted((FIXTURE_ROOT / subdirectory).glob("*.json"))


def test_demo_fixtures_exist() -> None:
    assert len(_fixtures("api")) >= 6
    assert len(_fixtures("s3")) >= 3


@pytest.mark.parametrize("path", _fixtures("api") + _fixtures("s3"), ids=lambda p: p.name)
def test_every_demo_fixture_matches_the_conversation_schema(path: Path) -> None:
    """A broken fixture would derail the live demo, so validate them all in CI."""
    ConversationInput.model_validate(json.loads(path.read_text()))


def test_storage_fixtures_ingest_end_to_end(
    ingestor: Ingestor, s3, session: Session, publisher: InMemoryPublisher
) -> None:
    """The real demo files, through the real ingestion path."""
    fixtures = _fixtures("s3")
    for path in fixtures:
        put(s3, path.name, json.loads(path.read_text()))

    result = ingestor.poll_once()

    assert result.ingested == len(fixtures)
    rows, total = list_conversations(session, source=JobSource.S3.value)
    assert total == len(fixtures)
    assert len(publisher.published) == len(fixtures)
    assert all(row.source_ref.startswith(f"s3://{BUCKET}/{PREFIX}") for row in rows)


def test_job_id_is_a_uuid(ingestor: Ingestor, s3, session: Session) -> None:
    put(s3, "a.json", sample_conversation())
    ingestor.poll_once()
    rows, _ = list_conversations(session)
    assert isinstance(rows[0].id, uuid.UUID)
    assert get_job(session, rows[0].id) is not None
