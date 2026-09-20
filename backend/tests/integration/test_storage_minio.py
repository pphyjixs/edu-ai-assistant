"""对象存储适配器的真实服务端验收（MinIO 或任意 S3 兼容服务）。

对应 plan 第 3 步的验收项：

1. 正确请求可以 PUT；
2. 内容与签名哈希不符被拒绝；
3. 重复 PUT 被拒绝（``If-None-Match: *`` 条件写入）；
4. HeadObject 读回大小、类型与存储侧校验值；
5. **后端进程不接收也不保存文件内容**——文件体由本测试直接 PUT 给存储服务，
   适配器只签发地址与读取元数据（用请求记录器断言它从未发过 PUT）。

运行方式（未配置端点时自动跳过，与数据库用例同一约定）::

    $env:TEST_S3_ENDPOINT="http://127.0.0.1:9000"
    $env:TEST_S3_BUCKET="edu-ai-test"
    $env:TEST_S3_ACCESS_KEY="minioadmin"
    $env:TEST_S3_SECRET_KEY="minioadmin"
    ..\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_storage_minio.py -q

或直接用一键脚本 ``scripts\\verify_storage.py``（会顺带创建测试桶）。
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

from app.storage import (
    S3Storage,
    S3StorageConfig,
    StorageObjectNotFoundError,
)

#: 上传的示例内容（bytes 只存在于本测试进程，不经后端）
SAMPLE_BODY = b"%PDF-1.7 fake chapter for storage acceptance\n"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@pytest.fixture(scope="session")
def s3_config() -> S3StorageConfig:
    """从环境变量读取测试用存储配置；未配置或不可达时跳过。"""
    endpoint = _env("TEST_S3_ENDPOINT")
    bucket = _env("TEST_S3_BUCKET")
    access_key = _env("TEST_S3_ACCESS_KEY")
    secret_key = _env("TEST_S3_SECRET_KEY")
    if not (endpoint and bucket and access_key and secret_key):
        pytest.skip(
            "未配置对象存储测试端点（TEST_S3_ENDPOINT / TEST_S3_BUCKET / "
            "TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY）"
        )

    config = S3StorageConfig(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=_env("TEST_S3_REGION", "us-east-1"),
        path_style=_env("TEST_S3_PATH_STYLE", "true").lower() != "false",
    )
    storage = S3Storage(config)
    try:
        storage.ensure_bucket()
    except Exception as exc:  # noqa: BLE001 - 端点不可达时按"环境未就绪"跳过
        pytest.skip(f"对象存储不可达，跳过 MinIO 验收：{type(exc).__name__}")
    finally:
        storage.close()
    return config


class _RecordingTransport(httpx.BaseTransport):
    """记录适配器发出的请求，用于证明后端从不代传文件内容。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.methods: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.method)
        return self._inner.handle_request(request)


@pytest.fixture
def recorder() -> _RecordingTransport:
    return _RecordingTransport(httpx.HTTPTransport())


@pytest.fixture
def storage(s3_config: S3StorageConfig, recorder: _RecordingTransport) -> Iterator[S3Storage]:
    client = httpx.Client(transport=recorder, timeout=10.0)
    instance = S3Storage(s3_config, client=client)
    try:
        yield instance
    finally:
        client.close()


def _object_key() -> str:
    """每次用例用独立键，避免互相干扰。"""
    return f"courses/{uuid.uuid4()}/uploads/{uuid.uuid4()}.pdf"


def _put(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
    """模拟浏览器直传：由测试直接 PUT，后端不参与。"""
    with httpx.Client(timeout=15.0) as client:
        return client.put(url, headers=headers, content=body)


def test_correct_presigned_put_succeeds(storage: S3Storage, recorder) -> None:
    """验收 1：签名头齐全的正确请求可以直传成功，并能被 HeadObject 确认。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()

    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )
    response = _put(upload.url, upload.headers, SAMPLE_BODY)

    assert response.status_code in (200, 201), response.text

    stored = storage.head_object(key)
    assert stored.size == len(SAMPLE_BODY)
    assert stored.content_type == "application/pdf"
    # ETag 与内容摘要不同：实现必须提供校验值通道，而不是拿 ETag 顶替
    assert (stored.etag or "").strip('"') != digest

    # 后端只发过 HEAD，从未代传文件内容
    assert "PUT" not in recorder.methods
    assert "POST" not in recorder.methods

    storage.delete_object(key)


def test_browser_put_preflight_allows_signed_headers(storage: S3Storage) -> None:
    """真实存储服务允许前端来源携带三个签名头发起 PUT。"""
    origin = _env("TEST_S3_CORS_ORIGIN", "http://localhost:5173")
    upload = storage.create_presigned_put(
        _object_key(),
        content_type="application/pdf",
        sha256_hex=hashlib.sha256(SAMPLE_BODY).hexdigest(),
    )
    requested_headers = ",".join(upload.headers)
    with httpx.Client(timeout=10.0) as client:
        response = client.options(
            upload.url,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert response.status_code in (200, 204), response.text
    assert response.headers.get("access-control-allow-origin") == origin
    assert "PUT" in response.headers.get("access-control-allow-methods", "").upper()
    allowed_headers = {
        name.strip().lower()
        for name in response.headers.get("access-control-allow-headers", "").split(",")
    }
    assert {name.lower() for name in upload.headers} <= allowed_headers


def test_head_object_reports_storage_checksum(storage: S3Storage) -> None:
    """HeadObject 能读回存储侧的 SHA-256 校验值（而非 ETag）。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()
    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )
    assert _put(upload.url, upload.headers, SAMPLE_BODY).status_code in (200, 201)

    stored = storage.head_object(key)
    try:
        if stored.checksum_sha256_hex() is None:
            # 契约 4.5：实现未返回该头时只记日志、不因此拒绝上传。
            # 这类实现无法验证"读回存储校验值"，明确标注而不是含糊通过。
            pytest.skip("该 S3 实现未在 HeadObject 返回 x-amz-checksum-sha256")
        assert stored.checksum_sha256_hex() == digest
    finally:
        storage.delete_object(key)


def test_checksum_mismatch_is_rejected(storage: S3Storage) -> None:
    """验收 2：内容与声明的 SHA-256 不符时存储服务拒绝写入。"""
    key = _object_key()
    wrong_digest = hashlib.sha256(b"another payload").hexdigest()

    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=wrong_digest
    )
    response = _put(upload.url, upload.headers, SAMPLE_BODY)

    if response.status_code in (200, 201):
        # 该实现没有校验 x-amz-checksum-sha256：本项无法在此服务端验证。
        # 适配器的责任是"把 Base64 摘要放进被签名的头"（unit 用例已断言），
        # 内容与声明是否一致由存储侧判定。
        storage.delete_object(key)
        pytest.skip("该 S3 实现未校验 x-amz-checksum-sha256，需在 MinIO/AWS S3 上验证本项")

    assert 400 <= response.status_code < 500, response.text
    assert response.status_code != 403, "签名本身应有效，拒绝必须来自校验失败"

    # 未写入任何对象
    with pytest.raises(StorageObjectNotFoundError):
        storage.head_object(key)


def test_duplicate_put_is_rejected(storage: S3Storage) -> None:
    """验收 3：``If-None-Match: *`` 条件写入使重复 PUT 被拒绝。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()

    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )
    assert _put(upload.url, upload.headers, SAMPLE_BODY).status_code in (200, 201)

    # 同一地址重复 PUT：对象已存在，必须被拒绝且不覆盖
    second = _put(upload.url, upload.headers, SAMPLE_BODY)
    assert second.status_code == 412, second.text

    # 重新签发地址再传同样内容，同样被拒绝
    again = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )
    third = _put(again.url, again.headers, b"%PDF-1.7 different body\n")
    assert third.status_code == 412, third.text

    stored = storage.head_object(key)
    # 大小未变即证明第二次/第三次 PUT 没有覆盖对象
    assert stored.size == len(SAMPLE_BODY)
    if stored.checksum_sha256_hex() is not None:
        assert stored.checksum_sha256_hex() == digest

    storage.delete_object(key)


def test_tampered_headers_invalidate_signature(storage: S3Storage) -> None:
    """被签名的头不可篡改：改了 Content-Type 或漏发校验头都应签名不符。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()
    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )

    tampered = dict(upload.headers)
    tampered["Content-Type"] = "text/plain"
    tampered_status = _put(upload.url, tampered, SAMPLE_BODY).status_code

    if tampered_status in (200, 201):
        storage.delete_object(key)
        pytest.skip("该 S3 实现未校验 SigV4 签名，需在 MinIO/AWS S3 上验证本项")

    assert tampered_status == 403, tampered_status

    missing = dict(upload.headers)
    missing.pop("x-amz-checksum-sha256")
    # 前一次篡改已被拒绝，对象尚未写入；漏发被签名的头同样应签名不符。
    # 不同 S3 实现对「缺失签名头」返回的状态码不同（MinIO 400 / AWS 403），
    # 关键断言是「被拒绝」，而不是具体状态码。
    response = _put(upload.url, missing, SAMPLE_BODY)
    assert response.status_code in (400, 403), response.text

    with pytest.raises(StorageObjectNotFoundError):
        storage.head_object(key)


def test_expired_presigned_url_is_rejected(storage: S3Storage) -> None:
    """过期地址不可用，客户端须重新初始化上传（契约 4.6）。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()

    upload = storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest, expires_in=1
    )
    time.sleep(2)
    response = _put(upload.url, upload.headers, SAMPLE_BODY)

    if response.status_code in (200, 201):
        storage.delete_object(key)
        pytest.skip("该 S3 实现未校验 X-Amz-Expires，需在 MinIO/AWS S3 上验证本项")

    # 过期地址必须被拒绝（不接受 2xx），具体状态码各实现不同（403 AccessDenied / 400）
    assert response.status_code in (400, 403), response.text


def test_adapter_records_no_content_transfer(storage: S3Storage, recorder) -> None:
    """后端进程不接收也不保存文件内容：适配器只发 HEAD，从不发 PUT/POST。"""
    key = _object_key()
    digest = hashlib.sha256(SAMPLE_BODY).hexdigest()

    storage.create_presigned_put(
        key, content_type="application/pdf", sha256_hex=digest
    )
    with pytest.raises(StorageObjectNotFoundError):
        storage.head_object(key)

    assert recorder.methods == ["HEAD"]
