"""S3-compatible object-lake client (Neon Object Storage, Tier 1).

The ETL pipeline's first stage writes every raw listing snapshot here
before anything else touches it, so ingestion can be replayed from the lake
whenever an extractor bug is fixed (Project-Description §4). Later phases
will also deliver generated PDFs through presigned URLs.

boto3 is synchronous; every call is dispatched via
:func:`asyncio.to_thread` so the event loop is never blocked — at this
stack's message volumes (hundreds of listings, not millions) thread
dispatch is well under one cold-start's cost, and it avoids a second S3
dependency (aioboto3).

Semantics chosen for the ETL consumer:

* ``get_json``/``get_bytes`` return ``None`` on a missing key — a miss is
  normal control flow (dedup checks), not an infrastructure failure;
* every *infrastructure* failure (auth, bucket, network, throttling)
  becomes :class:`~src.core.exceptions.StorageError` with the S3 status
  and error code in ``details``, retryable by default — a cold object
  store is expected to warm up.

The boto3 client is exposed as :meth:`StorageClient.raw` for operations
this wrapper does not cover (multipart uploads, lifecycle rules …).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import boto3
import botocore
from botocore.exceptions import BotoCoreError, ClientError

from src.core.config import Settings
from src.core.exceptions import ConfigurationError, StorageError
from src.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_REGION = "us-east-1"
_MISSING_KEY_CODES = {"NoSuchKey", "404", "NotFound"}


def _s3_error_details(error: ClientError) -> dict[str, Any]:
    response = error.response or {}
    metadata = response.get("ResponseMetadata", {})
    return {
        "s3_code": response.get("Error", {}).get("Code"),
        "status": metadata.get("HTTPStatusCode"),
        "request_id": metadata.get("RequestId"),
    }


class StorageClient:
    """Async facade over a boto3 S3 client for one bucket."""

    def __init__(self, s3: Any, *, bucket: str) -> None:
        self._s3 = s3
        self.bucket = bucket

    @property
    def raw(self) -> Any:
        """The underlying boto3 client (advanced operations only)."""
        return self._s3

    # ------------------------------------------------------------------
    # Sync core with error mapping (runs in a worker thread)
    # ------------------------------------------------------------------

    def _call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ClientError as error:
            raise StorageError(
                f"S3 call failed: {error}",
                details=_s3_error_details(error),
            ) from error
        except (BotoCoreError, OSError) as error:
            # Connection resets, DNS hiccups, unreachable endpoints: all
            # transient in the free-tier world.
            raise StorageError(
                f"S3 unreachable: {error}", details={"s3_code": "unreachable"}
            ) from error

    async def _acall(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._call, func, *args, **kwargs)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        await self._acall(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.debug("storage.put", key=key, size=len(data))

    async def put_json(self, key: str, value: Any) -> None:
        payload = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        await self.put_bytes(key, payload, content_type="application/json")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_bytes(self, key: str) -> bytes | None:
        """Object contents, or ``None`` when the key does not exist."""

        def _get() -> bytes | None:
            try:
                response = self._s3.get_object(Bucket=self.bucket, Key=key)
                return response["Body"].read()
            except ClientError as error:
                if _s3_error_details(error)["s3_code"] in _MISSING_KEY_CODES:
                    return None
                raise

        return await asyncio.to_thread(_get)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get_bytes(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StorageError(
                f"Object {key!r} is not valid JSON",
                details={"key": key, "parse_error": str(error)},
            ) from error

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError as error:
                if _s3_error_details(error)["status"] == 404:
                    return False
                raise

        return await asyncio.to_thread(_head)

    # ------------------------------------------------------------------
    # Listing / deletion
    # ------------------------------------------------------------------

    async def list_keys(self, prefix: str = "", *, limit: int = 1000) -> list[str]:
        """Keys under ``prefix`` (directories excluded), capped at ``limit``.

        Paginates internally so callers never depend on page sizes.
        """
        keys: list[str] = []
        continuation: str | None = None

        def _list_page() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": min(1000, limit - len(keys)),
            }
            if continuation:
                kwargs["ContinuationToken"] = continuation
            return self._s3.list_objects_v2(**kwargs)

        while len(keys) < limit:
            page = await asyncio.to_thread(self._call, _list_page)
            for entry in page.get("Contents", []):
                key = entry["Key"]
                if not key.endswith("/"):
                    keys.append(key)
            continuation = page.get("NextContinuationToken")
            if not continuation:
                break
        return keys[:limit]

    async def delete(self, key: str) -> None:
        await self._acall(self._s3.delete_object, Bucket=self.bucket, Key=key)

    # ------------------------------------------------------------------
    # Presigned delivery (PDFs in a later phase)
    # ------------------------------------------------------------------

    def presigned_get_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """Locally-signed time-limited GET URL (no network call)."""
        return self._call(
            self._s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


def make_storage_client(settings: Settings, *, s3: Any = None) -> StorageClient:
    """Build the wrapper; an injected ``s3`` client (tests) skips
    URL-based construction."""
    if s3 is None:
        missing = [
            name
            for name, value in {
                "S3_ENDPOINT_URL": settings.s3_endpoint_url,
                "S3_BUCKET": settings.s3_bucket,
                "S3_ACCESS_KEY_ID": settings._secret_set(settings.s3_access_key_id),
                "S3_SECRET_ACCESS_KEY": settings._secret_set(
                    settings.s3_secret_access_key
                ),
            }.items()
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Object storage is not configured; missing: {', '.join(missing)}",
                details={"missing": missing},
            )
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region or _DEFAULT_REGION,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        )
    return StorageClient(s3, bucket=settings.s3_bucket)
