"""The storage ingestion path.

Conversations dropped into object storage become exactly the same jobs as those submitted through
the API: same validation, same table, same queue, same worker. This process only turns objects
into jobs; it never scores anything.

Locally this polls. In production the natural shape is S3 ObjectCreated notifications into an SQS
queue, which removes the polling latency and the repeated LIST calls. Everything downstream is
unchanged, which is the point of normalising here.

Duplicate protection is a row per object keyed on (bucket, key, etag). Polling means we see the
same objects over and over, so without it every poll would create another job and another OpenAI
charge for the same conversation.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_engine, get_session_factory, wait_for_schema
from app.factories import build_queue, build_storage
from app.heartbeat import touch as touch_heartbeat
from app.logging import configure_logging, get_logger
from app.models import IngestStatus, JobSource
from app.queue import JobPublisher, QueuePublishError, SqsPublisher
from app.repository import (
    create_job,
    mark_enqueue_failed,
    mark_enqueued,
    object_already_seen,
    record_ingested_object,
)
from app.schemas import ConversationInput
from app.storage import ObjectStore, StorageUnavailableError, StoredObject

log = get_logger(__name__)

MAX_OBJECT_BYTES = 256 * 1024


@dataclass
class PollResult:
    """What one pass over the bucket did. These become metrics in milestone 7."""

    discovered: int = 0
    ingested: int = 0
    skipped_duplicate: int = 0
    invalid: int = 0
    failed: int = 0

    def __add__(self, other: PollResult) -> PollResult:
        return PollResult(
            discovered=self.discovered + other.discovered,
            ingested=self.ingested + other.ingested,
            skipped_duplicate=self.skipped_duplicate + other.skipped_duplicate,
            invalid=self.invalid + other.invalid,
            failed=self.failed + other.failed,
        )


class Ingestor:
    """Polls the incoming prefix and turns new objects into scoring jobs."""

    def __init__(
        self,
        settings: Settings,
        store: ObjectStore,
        publisher: JobPublisher,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.publisher = publisher
        self._session_factory = session_factory or (lambda: get_session_factory()())
        self._stop = False
        self.last_success: float | None = None

    # -- lifecycle ---------------------------------------------------------------------

    def request_stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        log.info("shutdown_requested", extra={"signal": signum})
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def run_forever(self) -> None:
        log.info(
            "ingestor_started",
            extra={
                "bucket": self.store.bucket,
                "prefix": self.store.prefix,
                "interval_seconds": self.settings.ingest_poll_interval_seconds,
            },
        )
        while not self._stop:
            try:
                result = self.poll_once()
                self.last_success = time.time()
                if result.ingested or result.invalid or result.failed:
                    log.info("ingest_poll", extra=vars(result))
            except StorageUnavailableError as exc:
                log.warning("storage_unavailable", extra={"error": str(exc)})
            except SQLAlchemyError as exc:
                log.error("database_error", extra={"error": str(exc)})
            except Exception:
                log.exception("ingest_poll_failed")
            self._sleep(self.settings.ingest_poll_interval_seconds)
        log.info("ingestor_stopped")

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    # -- one pass ----------------------------------------------------------------------

    def poll_once(self) -> PollResult:
        touch_heartbeat()
        objects = self.store.list_objects()
        result = PollResult(discovered=len(objects))
        for stored in objects:
            if self._stop:
                break
            result = result + self._handle(stored)
        return result

    def _handle(self, stored: StoredObject) -> PollResult:
        uri = self.store.uri(stored.key)

        with self._session() as session:
            if object_already_seen(session, self.store.bucket, stored.key, stored.etag):
                return PollResult(skipped_duplicate=1)

        # Read outside the transaction: object storage is slow and occasionally unavailable, and
        # holding a database transaction open across it would be wasteful.
        try:
            raw = self.store.read_object(stored.key)
        except StorageUnavailableError as exc:
            # Deliberately not recorded: a read failure is usually transient, so leave the object
            # to be picked up on the next poll rather than marking it permanently handled.
            log.warning("object_read_failed", extra={"object": uri, "error": str(exc)})
            return PollResult(failed=1)

        conversation, problem = self._parse(raw, stored)
        if conversation is None:
            self._record_invalid(stored, problem or "invalid object")
            log.warning("object_rejected", extra={"object": uri, "reason": problem})
            return PollResult(invalid=1)

        return self._create_job(stored, conversation, uri)

    def _parse(
        self, raw: bytes, stored: StoredObject
    ) -> tuple[ConversationInput | None, str | None]:
        """Validate an object with the same schema the API uses. One validator, one pipeline."""
        if len(raw) > MAX_OBJECT_BYTES:
            return None, f"object is {len(raw)} bytes, limit is {MAX_OBJECT_BYTES}"
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            return None, f"not valid JSON: {exc}"
        if not isinstance(payload, dict):
            return None, "expected a JSON object with a 'messages' array"
        try:
            return ConversationInput.model_validate(payload), None
        except ValidationError as exc:
            return None, f"does not match the conversation schema: {exc}"

    def _record_invalid(self, stored: StoredObject, error: str) -> None:
        try:
            with self._session() as session:
                record_ingested_object(
                    session,
                    bucket=self.store.bucket,
                    key=stored.key,
                    etag=stored.etag,
                    size_bytes=stored.size_bytes,
                    status=IngestStatus.INVALID,
                    error=error,
                )
        except IntegrityError:
            # Another replica recorded it first; nothing to do.
            pass

    def _create_job(
        self, stored: StoredObject, conversation: ConversationInput, uri: str
    ) -> PollResult:
        # The job row and the ingestion record are written in one transaction. Either this object
        # produced a job and is marked handled, or neither happened and the next poll retries it.
        try:
            with self._session() as session:
                job = create_job(
                    session,
                    conversation,
                    source=JobSource.S3,
                    max_attempts=self.settings.max_attempts,
                    source_ref=uri,
                )
                record_ingested_object(
                    session,
                    bucket=self.store.bucket,
                    key=stored.key,
                    etag=stored.etag,
                    size_bytes=stored.size_bytes,
                    status=IngestStatus.INGESTED,
                    conversation_id=job.id,
                )
                job_id = job.id
        except IntegrityError:
            # Lost a race with another replica for this exact object.
            log.info("object_claimed_by_another_ingestor", extra={"object": uri})
            return PollResult(skipped_duplicate=1)

        try:
            self.publisher.publish(job_id)
        except QueuePublishError as exc:
            # Same dual-write gap as the API, handled the same way: the job is marked failed so it
            # is visible rather than sitting pending forever.
            with self._session() as session:
                mark_enqueue_failed(session, job_id, str(exc))
            log.error("job_enqueue_failed", extra={"job_id": str(job_id), "object": uri})
            return PollResult(failed=1)

        with self._session() as session:
            mark_enqueued(session, job_id)

        log.info(
            "object_ingested",
            extra={
                "job_id": str(job_id),
                "source": JobSource.S3.value,
                "object": uri,
                "size_bytes": stored.size_bytes,
                "message_count": len(conversation.messages),
            },
        )
        return PollResult(ingested=1)


def main() -> int:
    settings = get_settings()
    configure_logging("ingestor", settings.log_level, settings.log_json)
    wait_for_schema(get_engine())

    ingestor = Ingestor(settings, build_storage(settings), SqsPublisher(build_queue(settings)))
    signal.signal(signal.SIGTERM, ingestor.request_stop)
    signal.signal(signal.SIGINT, ingestor.request_stop)
    ingestor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
