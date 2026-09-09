"""Object storage access for the ingestion path.

Only two operations are needed: list what is in the incoming prefix, and read one object. The
ingestor never writes to or deletes from the bucket, and its IAM policy grants nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.logging import get_logger

log = get_logger(__name__)


class StorageUnavailableError(RuntimeError):
    """The bucket could not be reached. The ingestor backs off and tries again."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    etag: str
    size_bytes: int

    @property
    def uri_suffix(self) -> str:
        return self.key


def build_s3_client(
    endpoint_url: str | None,
    region: str,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
            # Path-style addressing: LocalStack and other S3-compatible endpoints do not serve
            # virtual-host style buckets on localhost.
            s3={"addressing_style": "path"},
        ),
    )


class ObjectStore:
    """One bucket, one prefix."""

    def __init__(self, client: Any, bucket: str, prefix: str = "incoming/") -> None:
        self._client = client
        self.bucket = bucket
        self.prefix = prefix

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def list_objects(self, max_keys: int = 1000) -> list[StoredObject]:
        """List candidate objects under the prefix.

        Directory placeholder keys and zero-byte objects are skipped: they carry no conversation
        and would otherwise be recorded as invalid on every poll.
        """
        objects: list[StoredObject] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.bucket,
                Prefix=self.prefix,
                PaginationConfig={"MaxItems": max_keys},
            ):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key.endswith("/") or item.get("Size", 0) == 0:
                        continue
                    objects.append(
                        StoredObject(
                            key=key,
                            etag=str(item.get("ETag", "")).strip('"'),
                            size_bytes=int(item.get("Size", 0)),
                        )
                    )
        except Exception as exc:
            raise StorageUnavailableError(
                f"could not list s3://{self.bucket}/{self.prefix}: {exc}"
            ) from exc
        return objects

    def read_object(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:
            raise StorageUnavailableError(f"could not read {self.uri(key)}: {exc}") from exc
