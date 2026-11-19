"""S3-compatible object-lake client (Neon Object Storage, Tier 1)."""

from __future__ import annotations

import asyncio
import gzip
import json
from typing import Any, Iterable

import boto3
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


def _is_missing_error(error: ClientError) -> bool:
    details = _s3_error_details(error)
    return (
        details["s3_code"] in _MISSING_KEY_CODES
        or details["status"] == 404
    )


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
            raise StorageError(
                f"S3 unreachable: {error}",
                details={"s3_code": "unreachable"},
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

    async def put_json_gz(self, key: str, value: Any) -> None:
        raw = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=6)
        await self.put_bytes(key, compressed, content_type="application/gzip")

    async def put_jsonl_gz(self, key: str, rows: Iterable[Any]) -> int:
        """Write rows as gzipped JSONL and return the row count written."""
        lines: list[bytes] = []
        count = 0
        for row in rows:
            lines.append(
                json.dumps(
                    row, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            )
            count += 1
        raw = b"\n".join(lines) + (b"\n" if lines else b"")
        compressed = gzip.compress(raw, compresslevel=6)
        await self.put_bytes(key, compressed, content_type="application/gzip")
        return count

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
                if _is_missing_error(error):
                    return None
                raise

        return await asyncio.to_thread(self._call, _get)

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

    async def get_json_gz(self, key: str) -> Any | None:
        compressed = await self.get_bytes(key)
        if compressed is None:
            return None
        try:
            raw = gzip.decompress(compressed)
            return json.loads(raw)
        except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StorageError(
                f"Object {key!r} is not valid gzipped JSON",
                details={"key": key, "parse_error": str(error)},
            ) from error

    async def get_jsonl_gz(self, key: str) -> list[Any] | None:
        compressed = await self.get_bytes(key)
        if compressed is None:
            return None
        try:
            raw = gzip.decompress(compressed).decode("utf-8")
            return [
                json.loads(line)
                for line in raw.splitlines()
                if line.strip()
            ]
        except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StorageError(
                f"Object {key!r} is not valid gzipped JSONL",
                details={"key": key, "parse_error": str(error)},
            ) from error

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError as error:
                if _is_missing_error(error):
                    return False
                raise

        return await asyncio.to_thread(self._call, _head)

    # ------------------------------------------------------------------
    # Listing / deletion
    # ------------------------------------------------------------------

    async def list_keys(self, prefix: str = "", *, limit: int = 1000) -> list[str]:
        """Keys under ``prefix`` (directories excluded), capped at ``limit``."""
        if limit <= 0:
            return []
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
            page = await self._acall(_list_page)
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
    # Presigned delivery (PDFs / attachments)
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
    bucket = settings.s3_bucket
    if s3 is None:
        missing = [
            name
            for name, value in {
                "S3_ENDPOINT_URL": settings.s3_endpoint_url,
                "S3_BUCKET": bucket,
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
        # Non-None by the missing-settings check above; bound so the type
        # checker can see it.
        assert bucket is not None
        assert settings.s3_access_key_id is not None
        assert settings.s3_secret_access_key is not None
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region or _DEFAULT_REGION,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        )
    if bucket is None:
        # Only reachable via an injected ``s3`` without a configured bucket
        # (tests always set one); the wrapper needs a bucket to operate.
        raise ConfigurationError(
            "Object storage is not configured; missing: S3_BUCKET",
            details={"missing": ["S3_BUCKET"]},
        )
    return StorageClient(s3, bucket=bucket)