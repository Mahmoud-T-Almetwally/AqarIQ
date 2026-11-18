"""Tests for src/services/storage_client.py (botocore Stubber, no network)."""

from __future__ import annotations

import io
import json

import pytest
import boto3
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from src.core.exceptions import ConfigurationError, StorageError
from src.services.storage_client import make_storage_client

BUCKET = "rea-lake"


@pytest.fixture
def s3():
    return boto3.client(
        "s3",
        endpoint_url="https://storage.neon.tech",
        region_name="eu-central-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )


@pytest.fixture
def client(s3, make_settings):
    return make_storage_client(
        make_settings(s3_bucket=BUCKET, s3_region="eu-central-1"),
        s3=s3,
    )


def _streaming(payload: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(payload), len(payload))


class TestWrites:
    async def test_put_bytes(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": BUCKET,
                    "Key": "raw/1.bin",
                    "Body": b"\x00\x01",
                    "ContentType": "application/octet-stream",
                },
            )
            await client.put_bytes("raw/1.bin", b"\x00\x01")

    async def test_put_json_compact_and_typed(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": BUCKET,
                    "Key": "raw/1.json",
                    "Body": '{"a":"ä"}'.encode("utf-8"),
                    "ContentType": "application/json",
                },
            )
            await client.put_json("raw/1.json", {"a": "ä"})


class TestReads:
    async def test_get_bytes_roundtrip(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "get_object",
                {"Body": _streaming(b"hello lake")},
                {"Bucket": BUCKET, "Key": "raw/1.bin"},
            )
            assert await client.get_bytes("raw/1.bin") == b"hello lake"

    async def test_get_bytes_missing_is_none(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_client_error(
                "get_object",
                service_error_code="NoSuchKey",
                http_status_code=404,
                expected_params={"Bucket": BUCKET, "Key": "raw/none.bin"},
            )
            assert await client.get_bytes("raw/none.bin") is None

    async def test_get_json_roundtrip(self, client, s3):
        payload = json.dumps({"city": "München"}).encode()
        with Stubber(s3) as stub:
            stub.add_response(
                "get_object",
                {"Body": _streaming(payload)},
                {"Bucket": BUCKET, "Key": "raw/1.json"},
            )
            assert await client.get_json("raw/1.json") == {"city": "München"}

    async def test_get_json_corrupt_raises(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "get_object",
                {"Body": _streaming(b"{oops")},
                {"Bucket": BUCKET, "Key": "raw/bad.json"},
            )
            with pytest.raises(StorageError, match="not valid JSON"):
                await client.get_json("raw/bad.json")

    async def test_exists_true(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "head_object", {}, {"Bucket": BUCKET, "Key": "raw/1.json"}
            )
            assert await client.exists("raw/1.json") is True

    async def test_exists_false(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_client_error(
                "head_object",
                service_error_code="404",
                http_status_code=404,
                expected_params={"Bucket": BUCKET, "Key": "raw/gone.json"},
            )
            assert await client.exists("raw/gone.json") is False

    async def test_get_bytes_non_404_raises_storage_error(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_client_error(
                "get_object",
                service_error_code="AccessDenied",
                http_status_code=403,
                expected_params={"Bucket": BUCKET, "Key": "raw/secret.bin"},
            )
            with pytest.raises(StorageError) as exc_info:
                await client.get_bytes("raw/secret.bin")
        assert exc_info.value.details["status"] == 403

    async def test_exists_non_404_raises_storage_error(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_client_error(
                "head_object",
                service_error_code="AccessDenied",
                http_status_code=403,
                expected_params={"Bucket": BUCKET, "Key": "raw/secret.bin"},
            )
            with pytest.raises(StorageError):
                await client.exists("raw/secret.bin")


class TestListingAndDeletion:
    async def test_list_keys_paginates_and_skips_directories(
        self, client, s3
    ):
        with Stubber(s3) as stub:
            stub.add_response(
                "list_objects_v2",
                {
                    "Contents": [
                        {"Key": "raw/1.json"},
                        {"Key": "raw/subdir/"},
                    ],
                    "NextContinuationToken": "page-2",
                    "IsTruncated": True,
                },
                {"Bucket": BUCKET, "Prefix": "raw/", "MaxKeys": 1000},
            )
            stub.add_response(
                "list_objects_v2",
                {"Contents": [{"Key": "raw/2.json"}]},
                {
                    "Bucket": BUCKET,
                    "Prefix": "raw/",
                    "MaxKeys": 999,
                    "ContinuationToken": "page-2",
                },
            )
            keys = await client.list_keys("raw/")
        assert keys == ["raw/1.json", "raw/2.json"]

    async def test_list_keys_respects_limit(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "list_objects_v2",
                {"Contents": [{"Key": "raw/1.json"}, {"Key": "raw/2.json"}]},
                {"Bucket": BUCKET, "Prefix": "raw/", "MaxKeys": 1},
            )
            keys = await client.list_keys("raw/", limit=1)
        # The page may return more items than MaxKeys; our contract is:
        # never more than `limit`.
        assert keys == ["raw/1.json"]

    async def test_delete(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_response(
                "delete_object", {}, {"Bucket": BUCKET, "Key": "raw/1.json"}
            )
            await client.delete("raw/1.json")

    async def test_json_gz_roundtrip(self, client, s3):
        import gzip
        payload = gzip.compress(b'{"city":"cairo"}')
        with Stubber(s3) as stub:
            stub.add_response(
                "get_object",
                {"Body": _streaming(payload)},
                {"Bucket": BUCKET, "Key": "raw/batch.json.gz"},
            )
            assert await client.get_json_gz("raw/batch.json.gz") == {"city": "cairoc"}

    async def test_jsonl_gz_roundtrip(self, client, s3):
        import gzip
        payload = gzip.compress(b'{"id":1}\n{"id":2}\n')
        with Stubber(s3) as stub:
            stub.add_response(
                "get_object",
                {"Body": _streaming(payload)},
                {"Bucket": BUCKET, "Key": "raw/batch.jsonl.gz"},
            )
            assert await client.get_jsonl_gz("raw/batch.jsonl.gz") == [{"id": 1}, {"id": 2}]

    async def test_list_keys_non_positive_limit_returns_empty(self, client):
        assert await client.list_keys("raw/", limit=0) == []


class TestErrorMapping:
    async def test_client_error_becomes_storage_error(self, client, s3):
        with Stubber(s3) as stub:
            stub.add_client_error(
                "put_object",
                service_error_code="AccessDenied",
                http_status_code=403,
                expected_params={
                    "Bucket": BUCKET,
                    "Key": "raw/x.json",
                    "Body": b'{"x":1}',
                    "ContentType": "application/json",
                },
            )
            with pytest.raises(StorageError) as exc_info:
                await client.put_json("raw/x.json", {"x": 1})
        assert exc_info.value.details["status"] == 403
        assert exc_info.value.details["s3_code"] == "AccessDenied"

    async def test_connection_error_is_retryable(self, s3, make_settings):
        client = make_storage_client(make_settings(s3_bucket=BUCKET), s3=s3)

        def _raise(*args, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://x.test")

        s3.put_object = _raise
        with pytest.raises(StorageError, match="unreachable") as exc_info:
            await client.put_bytes("k", b"v")
        assert exc_info.value.retryable is True


class TestPresignedUrls:
    def test_contains_key_and_signature(self, client):
        url = client.presigned_get_url("reports/42.pdf", expires_seconds=900)
        assert "reports%2F42.pdf" in url or "reports/42.pdf" in url
        assert "X-Amz-Signature" in url
        assert "X-Amz-Expires=900" in url


class TestFactory:
    def test_missing_config_lists_fields(self, make_settings):
        with pytest.raises(ConfigurationError) as exc_info:
            make_storage_client(make_settings())
        message = str(exc_info.value)
        for field in ("S3_ENDPOINT_URL", "S3_BUCKET"):
            assert field in message

    async def test_bucket_from_settings(self, client):
        assert client.bucket == BUCKET
