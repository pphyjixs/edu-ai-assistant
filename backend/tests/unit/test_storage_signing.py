"""对象存储适配器的离线单元测试（不访问网络）。

覆盖 plan 第 3 步中不依赖真实 S3 服务的部分：

- SigV4 签名密钥的已知答案向量（AWS 官方示例）；
- 预签名 URL 的参数、被签名的请求头与到期时间；
- 对象键不含用户文件名；
- HeadObject 的状态码映射（404 / 403 / 5xx / 超时）与 ``503 SERVICE_UNAVAILABLE`` 转换；
- 适配器**不提供**任何接收文件内容的入口。

真实 MinIO 上的端到端校验在 ``tests/integration/test_storage_minio.py``。
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import uuid
from datetime import timedelta

import httpx
import pytest

from app.core.error_codes import ErrorCode
from app.core.time import utc_now
from app.storage import (
    InvalidExtensionError,
    S3Storage,
    S3StorageConfig,
    StorageConfigError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    as_service_unavailable,
    build_upload_object_key,
    close_storages,
    get_storage,
    normalize_extension,
    sha256_base64,
)
from app.storage.s3 import signing_key

#: 测试用存储配置（凭据是 MinIO 默认值，不是真实密钥）
TEST_CONFIG = S3StorageConfig(
    endpoint="http://127.0.0.1:9000",
    bucket="edu-ai-materials",
    access_key="minioadmin",
    secret_key="minioadmin",
    region="us-east-1",
)

#: 一段固定的摘要，避免测试里到处算
SAMPLE_HEX = hashlib.sha256(b"chapter-1").hexdigest()


def test_head_request_is_sigv4_signed() -> None:
    """HeadObject 必须签名并请求返回存储侧校验值。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/edu-ai-materials/courses/x.pdf"
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
        assert request.headers["x-amz-checksum-mode"] == "ENABLED"
        signed_names = request.headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
        assert "x-amz-checksum-mode" in signed_names.split(";")
        assert request.headers["authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=minioadmin/"
        )
        return httpx.Response(200, headers={"content-length": "3"})

    storage = S3Storage(TEST_CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
    stored = storage.head_object("courses/x.pdf")
    assert stored.size == 3


# --------------------------------------------------------------------------- #
# SigV4 签名
# --------------------------------------------------------------------------- #
def test_signing_key_matches_aws_documented_vector() -> None:
    """AWS 文档中的签名密钥示例：20150830 / us-east-1 / iam。"""
    key = signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "20150830",
        "us-east-1",
        "iam",
    )

    assert key.hex() == (
        "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"
    )


def test_sha256_base64_matches_standard_encoding() -> None:
    expected = base64.b64encode(bytes.fromhex(SAMPLE_HEX)).decode("ascii")

    assert sha256_base64(SAMPLE_HEX) == expected
    # 大小写与首尾空白都被容忍，统一按小写处理
    assert sha256_base64(f"  {SAMPLE_HEX.upper()}  ") == expected


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, SAMPLE_HEX[:-1]])
def test_sha256_base64_rejects_invalid_hex(bad: str) -> None:
    with pytest.raises(StorageConfigError):
        sha256_base64(bad)


# --------------------------------------------------------------------------- #
# 预签名 PUT
# --------------------------------------------------------------------------- #
def test_presigned_put_signs_required_headers() -> None:
    storage = S3Storage(TEST_CONFIG)

    upload = storage.create_presigned_put(
        "courses/abc/uploads/def.pdf",
        content_type="application/pdf",
        sha256_hex=SAMPLE_HEX,
    )

    assert upload.method == "PUT"
    assert upload.headers == {
        "Content-Type": "application/pdf",
        "If-None-Match": "*",
        "x-amz-checksum-sha256": sha256_base64(SAMPLE_HEX),
    }

    url = upload.url
    assert url.startswith("http://127.0.0.1:9000/edu-ai-materials/courses/abc/uploads/def.pdf?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Credential=minioadmin%2F" in url
    assert "X-Amz-Expires=600" in url
    assert "X-Amz-Signature=" in url
    # 三个头 + host 都要参与签名，否则客户端漏发/改发时签名不会失效
    assert "X-Amz-SignedHeaders=content-type%3Bhost%3Bif-none-match%3Bx-amz-checksum-sha256" in url


def test_presigned_put_expiry_is_url_ttl_not_confirm_window() -> None:
    storage = S3Storage(TEST_CONFIG)

    upload = storage.create_presigned_put(
        "courses/abc/uploads/def.pdf",
        content_type="application/pdf",
        sha256_hex=SAMPLE_HEX,
    )

    remaining = upload.expires_at - utc_now()
    assert timedelta(seconds=595) < remaining <= timedelta(seconds=600)
    assert upload.checksum_sha256_base64 == sha256_base64(SAMPLE_HEX)


def test_presigned_put_does_not_touch_the_network() -> None:
    """签发必须是纯本地计算：后端不参与文件传输。"""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("签发预签名地址不应发起任何 HTTP 请求")

    storage = S3Storage(
        TEST_CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    storage.create_presigned_put(
        "courses/abc/uploads/def.pdf",
        content_type="application/pdf",
        sha256_hex=SAMPLE_HEX,
    )


def test_presigned_put_requires_configuration() -> None:
    storage = S3Storage(S3StorageConfig(endpoint="", bucket="", access_key="", secret_key=""))

    with pytest.raises(StorageUnavailableError) as excinfo:
        storage.create_presigned_put(
            "courses/abc/uploads/def.pdf",
            content_type="application/pdf",
            sha256_hex=SAMPLE_HEX,
        )

    assert excinfo.value.reason == "not_configured"
    # 说明里只报缺失的变量名，不回显任何取值
    assert "STORAGE_ENDPOINT" in str(excinfo.value)


def test_adapter_never_accepts_file_content() -> None:
    """适配器不提供任何接收文件内容的入口（契约：文件不经后端）。"""
    public_methods = {
        name
        for name, _ in inspect.getmembers(S3Storage, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {
        "bucket_exists",
        "close",
        "create_presigned_put",
        "delete_object",
        "ensure_bucket",
        "head_object",
    }

    forbidden = {"body", "content", "data", "file", "stream", "payload", "bytes"}
    for name in public_methods:
        parameters = set(inspect.signature(getattr(S3Storage, name)).parameters)
        assert not (parameters & forbidden), f"{name} 不应接收文件内容参数"


# --------------------------------------------------------------------------- #
# 对象键
# --------------------------------------------------------------------------- #
def test_object_key_contains_only_backend_generated_parts() -> None:
    course_id = uuid.uuid4()
    upload_id = uuid.uuid4()

    key = build_upload_object_key(course_id, upload_id, ".pdf")

    assert key == f"courses/{course_id}/uploads/{upload_id}.pdf"
    # 不含用户文件名、路径分隔符或用户可控片段
    assert "chapter-1" not in key
    assert key.count("/") == 3


def test_object_key_normalizes_extension_case() -> None:
    course_id, upload_id = uuid.uuid4(), uuid.uuid4()

    assert build_upload_object_key(course_id, upload_id, "PPTX").endswith(".pptx")


@pytest.mark.parametrize("bad", ["", " ", "../x", ".p df", ".p/df", "pdf/../x"])
def test_object_key_rejects_unsafe_extension(bad: str) -> None:
    with pytest.raises(InvalidExtensionError):
        normalize_extension(bad)


# --------------------------------------------------------------------------- #
# HeadObject 与错误映射
# --------------------------------------------------------------------------- #
def _storage_with(handler) -> S3Storage:
    return S3Storage(TEST_CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_head_object_reads_size_type_and_checksum() -> None:
    checksum_b64 = sha256_base64(SAMPLE_HEX)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": "1048576",
                "content-type": "application/pdf",
                "x-amz-checksum-sha256": checksum_b64,
                "etag": '"d41d8cd98f00b204e9800998ecf8427e"',
            },
        )

    stored = _storage_with(handler).head_object("courses/abc/uploads/def.pdf")

    assert stored.size == 1048576
    assert stored.content_type == "application/pdf"
    assert stored.checksum_sha256_hex() == SAMPLE_HEX
    # ETag 与内容摘要不同：不得把 ETag 当作 SHA-256
    assert stored.etag != SAMPLE_HEX


def test_head_object_without_stored_checksum_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "10", "etag": '"abc"'})

    stored = _storage_with(handler).head_object("courses/abc/uploads/def.pdf")

    assert stored.checksum_sha256_hex() is None


def test_head_object_missing_object_raises_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(StorageObjectNotFoundError):
        _storage_with(handler).head_object("courses/abc/uploads/def.pdf")


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "access_denied"), (500, "server_error"), (503, "server_error")],
)
def test_head_object_transient_failures_map_to_503(status: int, reason: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(StorageUnavailableError) as excinfo:
        _storage_with(handler).head_object("courses/abc/uploads/def.pdf")

    api_error = as_service_unavailable(excinfo.value)
    assert excinfo.value.reason == reason
    assert api_error.status_code == 503
    assert api_error.code is ErrorCode.SERVICE_UNAVAILABLE
    assert api_error.details == {"component": "storage", "reason": reason}
    # 对外文案不含 endpoint、桶名或响应正文
    assert "127.0.0.1" not in api_error.message


def test_head_object_timeout_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(StorageUnavailableError) as excinfo:
        _storage_with(handler).head_object("courses/abc/uploads/def.pdf")

    assert excinfo.value.reason == "timeout"


def test_head_object_connection_error_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(StorageUnavailableError) as excinfo:
        _storage_with(handler).head_object("courses/abc/uploads/def.pdf")

    assert excinfo.value.reason == "connection"


def test_delete_object_reports_missing_as_false() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _storage_with(handler).delete_object("courses/abc/uploads/def.pdf") is False


def test_get_storage_caches_by_configuration(make_settings) -> None:
    """同一份存储配置共用一个适配器（避免每请求新建连接池）。"""
    settings = make_settings(
        storage_endpoint="http://127.0.0.1:9000",
        storage_bucket="edu-ai-materials",
        storage_access_key="minioadmin",
        storage_secret_key="minioadmin",
    )

    try:
        first = get_storage(settings)
        second = get_storage(settings)
        assert first is second
    finally:
        close_storages()
