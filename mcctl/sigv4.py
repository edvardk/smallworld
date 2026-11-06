"""AWS Signature Version 4 for one request: the HeadBucket / ListObjectsV2 check of a new R2 state token
(init step 4).

Stdlib only (hashlib/hmac). Checked against the AWS SigV4 test suite vector `get-vanilla` (tests). R2 takes
region `auto` and service `s3`; credentials derived from a Cloudflare API token are access key id = token
id, secret = SHA-256 hex of the token value (what Terraform does for the backup token too).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping

from mcctl import http

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def authorization(
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime.datetime,
    payload_hash: str = EMPTY_SHA256,
) -> str:
    """The Authorization header value; `headers` are the headers to sign (must include host and
    x-amz-date)."""
    parts = urllib.parse.urlsplit(url)
    canonical_headers = {k.lower(): " ".join(v.split()) for k, v in headers.items()}
    signed = ";".join(sorted(canonical_headers))
    query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    creq = "\n".join(
        [
            method,
            urllib.parse.quote(parts.path or "/", safe="/-_.~"),
            query,
            "".join(f"{k}:{canonical_headers[k]}\n" for k in sorted(canonical_headers)),
            signed,
            payload_hash,
        ]
    )
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{region}/{service}/aws4_request"
    sts = "\n".join(
        ["AWS4-HMAC-SHA256", now.strftime("%Y%m%dT%H%M%SZ"), scope, hashlib.sha256(creq.encode()).hexdigest()]
    )
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"


def _signed(
    method: str, url: str, access_key: str, secret_key: str, now: datetime.datetime
) -> dict[str, str]:
    host = urllib.parse.urlsplit(url).netloc
    now = now.astimezone(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    signed = {"host": host, "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": amz_date}
    auth = authorization(
        method,
        url,
        signed,
        access_key=access_key,
        secret_key=secret_key,
        region="auto",
        service="s3",
        now=now,
    )
    return {"x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": amz_date, "Authorization": auth}


def head_bucket(
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    transport: http.Transport,
    now: datetime.datetime,
) -> int:
    """HTTP status of `HEAD <endpoint>/<bucket>` (200 = the credentials can use the bucket)."""
    url = f"{endpoint.rstrip('/')}/{urllib.parse.quote(bucket)}"
    return transport(http.Request("HEAD", url, _signed("HEAD", url, access_key, secret_key, now))).status


def list_objects(
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    transport: http.Transport,
    now: datetime.datetime,
) -> int:
    """HTTP status of `GET <endpoint>/<bucket>?list-type=2&max-keys=1` (ListObjectsV2). An object-scoped R2
    token may be refused HeadBucket (a bucket-level call) yet list and write objects, which is all
    Terraform's S3 backend needs."""
    url = f"{endpoint.rstrip('/')}/{urllib.parse.quote(bucket)}?list-type=2&max-keys=1"
    return transport(http.Request("GET", url, _signed("GET", url, access_key, secret_key, now))).status


def bucket_usable(
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    transport: http.Transport,
    now: datetime.datetime,
) -> int:
    """HeadBucket, falling back to ListObjectsV2 when HeadBucket is forbidden: 200 = usable."""
    status = head_bucket(endpoint, bucket, access_key, secret_key, transport, now)
    if status in (401, 403):
        status = list_objects(endpoint, bucket, access_key, secret_key, transport, now)
    return status
