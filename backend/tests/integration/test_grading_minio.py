"""提交与批改的真实对象存储端到端测试（``docs/api-contract.md`` 第 9 节 + 真实 MinIO）。

与 ``test_grading_api.py``（内存假存储）互补：这里用**真实 MinIO**验证报告直传
的完整链路——初始化上传 → 浏览器直传（真实预签名 PUT）→ 完成确认（HeadObject
校验大小 / MIME / 存储侧 SHA-256）→ 预签名 GET 下载回读 → 被替代会话的对象清理，
以及 Worker 从真实桶读取报告并批改。

需要配置 ``TEST_S3_ENDPOINT`` / ``TEST_S3_BUCKET`` / ``TEST_S3_ACCESS_KEY`` /
``TEST_S3_SECRET_KEY``（端点不可达时整组跳过；桶按需创建）。
**模型为本地假 HTTP 服务（``httpx.MockTransport``），真实模型效果未验收。**
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.modules.grading import service as grading_service
from app.modules.grading import worker
from app.storage import S3Storage, S3StorageConfig
from app.storage.deps import get_storage_dep
from tests.integration.test_grading_api import (
    COMPLETE_URL,
    REPORT_LINES,
    UPLOADS_URL,
    _auth,
    _grading_settings,
    _open_assignment,
    build_pdf,
    fake_model_response,
)

PDF_MIME = "application/pdf"

#: 报告正文（ASCII，保证 pypdf 提取后证据摘录可核对）
REPORT_BODY = build_pdf(REPORT_LINES)
REPORT_SHA256 = hashlib.sha256(REPORT_BODY).hexdigest()
REPORT_SIZE = len(REPORT_BODY)


@pytest.fixture(scope="module")
def real_storage() -> Iterator[S3Storage]:
    """真实 MinIO 适配器；未配置或不可达时整组跳过。"""
    endpoint = os.environ.get("TEST_S3_ENDPOINT", "").strip()
    bucket = os.environ.get("TEST_S3_BUCKET", "").strip()
    access_key = os.environ.get("TEST_S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("TEST_S3_SECRET_KEY", "").strip()
    if not (endpoint and bucket and access_key and secret_key):
        pytest.skip(
            "未配置对象存储测试端点（TEST_S3_ENDPOINT / TEST_S3_BUCKET / "
            "TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY）"
        )
    storage = S3Storage(
        S3StorageConfig(
            endpoint=endpoint.rstrip("/"),
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )
    )
    try:
        storage.ensure_bucket()
        yield storage
    finally:
        storage.close()


@pytest.fixture
def client(db_isolation: None, pg_app, real_storage: S3Storage) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: real_storage
    with TestClient(app) as test_client:
        yield test_client


def _put(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
    """模拟浏览器直传：文件体不经过后端。"""
    with httpx.Client(timeout=30.0) as client:
        return client.put(url, headers=headers, content=body)


def _get(url: str) -> httpx.Response:
    with httpx.Client(timeout=30.0) as client:
        return client.get(url)


def _init_upload(
    client: TestClient, token: str, assignment_id: str, *, body: bytes = REPORT_BODY
) -> dict:
    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment_id),
        json={
            "filename": "report.pdf",
            "content_type": PDF_MIME,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _direct_upload_and_submit(
    client: TestClient, token: str, assignment_id: str, *, body: bytes = REPORT_BODY
) -> dict:
    """真实链路：初始化 → 直传 → 完成确认，返回提交详情。"""
    init = _init_upload(client, token, assignment_id)
    uploaded = _put(init["upload_url"], init["headers"], body)
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=init["upload_id"]),
        headers=_auth(token),
    )
    assert completed.status_code == 201, completed.text
    return completed.json()


def _object_key_of(storage: S3Storage, init: dict) -> str:
    """从预签名地址解析对象键（path-style：``/bucket/key``）。"""
    path = httpx.URL(init["upload_url"]).path.lstrip("/")
    bucket = storage.config.bucket
    assert path.startswith(f"{bucket}/")
    return path[len(bucket) + 1 :]


def test_direct_put_and_presigned_download_roundtrip(client) -> None:
    """直传 → 完成确认 → 预签名 GET 读回同样的字节。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    detail = _direct_upload_and_submit(client, student, assignment["id"])

    assert detail["download_url"]
    downloaded = _get(detail["download_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == REPORT_BODY


def test_presigned_put_rejects_second_write_with_same_key(client) -> None:
    """条件写入（``If-None-Match: *``）：同一对象键的重复 PUT 被拒绝。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    init = _init_upload(client, student, assignment["id"])

    assert _put(init["upload_url"], init["headers"], REPORT_BODY).status_code == 200
    again = _put(init["upload_url"], init["headers"], b"%PDF-1.4 again\n")
    assert again.status_code == 412, again.text


def test_complete_rejects_checksum_mismatch_from_real_storage(client) -> None:
    """直传内容与声明不符：存储侧拒绝，完成确认按对象缺失拒绝。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    other = b"%PDF-1.4 different body\n"
    init = _init_upload(client, student, assignment["id"], body=other)

    uploaded = _put(init["upload_url"], init["headers"], b"%PDF-1.4 tampered\n")
    assert uploaded.status_code in (400, 412), uploaded.text

    completed = client.post(
        COMPLETE_URL.format(assignment_id=assignment["id"], upload_id=init["upload_id"]),
        headers=_auth(student),
    )
    assert completed.status_code == 422, completed.text
    assert completed.json()["error"]["code"] == "UPLOAD_INVALID"


def test_worker_grades_report_from_real_bucket(
    client, real_storage, pg_session_factory, make_settings
) -> None:
    """端到端：真实桶 → Worker 下载并复核 → 假模型批改 → REVIEW_REQUIRED。"""
    settings: Settings = _grading_settings(make_settings)
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _direct_upload_and_submit(client, student, assignment["id"])
    triggered = client.post(
        f"/api/v1/submissions/{detail['id']}/grade", headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text

    processed = asyncio.run(
        worker.run_pending_batch(
            pg_session_factory,
            settings=settings,
            storage=real_storage,
            ai_client_factory=lambda: httpx.Client(
                transport=fake_model_response(), timeout=10.0
            ),
            max_jobs=1,
        )
    )

    assert processed == 1
    review = client.get(
        f"/api/v1/submissions/{detail['id']}/grade-review", headers=_auth(teacher)
    )
    assert review.status_code == 200, review.text
    assert review.json()["final_total_score"] == 20.0


def test_superseded_upload_object_is_cleaned_from_real_bucket(
    client, real_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """重新初始化后的旧对象由清理命令从真实桶中移除，新对象保留。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    first = _init_upload(client, student, assignment["id"])
    assert _put(first["upload_url"], first["headers"], REPORT_BODY).status_code == 200
    second = _init_upload(client, student, assignment["id"])
    assert _put(second["upload_url"], second["headers"], REPORT_BODY).status_code == 200

    # 预签名 PUT 已过期并超过删除缓冲期，可以安全删除
    _backdate_put_expiry(pg_sync_engine, [first["upload_id"]])

    async def cleanup() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=real_storage,
                settings=_grading_settings(make_settings),
            )

    assert asyncio.run(cleanup()) == 1

    with pytest.raises(Exception):
        real_storage.head_object(_object_key_of(real_storage, first))
    assert real_storage.head_object(_object_key_of(real_storage, second)).size == (
        REPORT_SIZE
    )


def _backdate_put_expiry(engine, upload_ids: list[str]) -> None:
    """把指定上传会话的 PUT 到期时间拨回 2 小时前（越过删除缓冲期）。"""
    from sqlalchemy import text

    with engine.begin() as connection:
        for upload_id in upload_ids:
            connection.execute(
                text(
                    "UPDATE submission_upload_sessions"
                    " SET upload_url_expires_at = now() - interval '2 hours'"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            )


def test_cors_preflight_allows_direct_upload_methods_and_headers(client) -> None:
    """契约 9.1：预检必须放行直传所需的 ``PUT`` / ``HEAD`` 与签名请求头。"""
    from app.core.cors import ALLOWED_HEADERS, ALLOWED_METHODS

    assert "PUT" in ALLOWED_METHODS
    assert "HEAD" in ALLOWED_METHODS
    lowered = {header.lower() for header in ALLOWED_HEADERS}
    assert "x-amz-checksum-sha256" in lowered
    assert "if-none-match" in lowered

    preflight = client.options(
        "/health/live",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": (
                "content-type,x-amz-checksum-sha256,if-none-match"
            ),
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert (
        preflight.headers["access-control-allow-headers"].lower()
        .find("x-amz-checksum-sha256")
        != -1
    )
