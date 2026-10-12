"""Object storage (S3 protocol). Works with SeaweedFS locally and S3-compatible cloud storage later:
only the endpoint and credentials change."""
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:8333")
BUCKET = os.environ.get("S3_BUCKET", "lectures")

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "dev"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "dev"),
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                # newer boto3 adds extra checksums that some S3-compatible servers don't support
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
    return _client


def ensure_bucket(retries=30):
    """Create the bucket if needed, waiting for storage to come up (it may start after us)."""
    for attempt in range(1, retries + 1):
        try:
            client().head_bucket(Bucket=BUCKET)
            return
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
                try:
                    client().create_bucket(Bucket=BUCKET)
                except ClientError as ce:
                    if ce.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise
                return
            print(f"Storage not ready ({attempt}/{retries}): {e}")
        except EndpointConnectionError as e:
            print(f"Storage not ready ({attempt}/{retries}): {e}")
        time.sleep(2)
    raise RuntimeError("Could not reach object storage")


def upload(fileobj, key, content_type="application/octet-stream"):
    """Stream a file into storage (sent in parts, so big videos never sit in memory)."""
    client().upload_fileobj(fileobj, BUCKET, key, ExtraArgs={"ContentType": content_type})


def download(key, path):
    client().download_file(BUCKET, key, str(path))


def head(key):
    return client().head_object(Bucket=BUCKET, Key=key)


def get(key, byte_range=None):
    kwargs = {"Bucket": BUCKET, "Key": key}
    if byte_range:
        kwargs["Range"] = byte_range
    return client().get_object(**kwargs)
