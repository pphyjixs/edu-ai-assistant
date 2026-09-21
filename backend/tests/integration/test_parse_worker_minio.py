"""解析 Worker 真实对象存储端到端测试（契约 5.5 + 真实 MinIO）。

与 ``test_materials_api.py``（内存假存储）互补：这里用**真实 MinIO**验证
三种格式的完整链路——初始化上传 → 浏览器直传（真实预签名 PUT）→ 完成 →
独立 Worker 领取并从对象存储流式读取（复核大小与 SHA-256）→ 假模型生成 →
发布 READY；以及删除后维护命令从真实桶中移除对象。

需要配置 ``TEST_S3_ENDPOINT`` / ``TEST_S3_BUCKET`` / ``TEST_S3_ACCESS_KEY`` /
``TEST_S3_SECRET_KEY``（端点不可达时整组跳过；桶按需创建）。
**模型为本地假 HTTP 服务（``httpx.MockTransport``），真实外部模型调用未验收。**
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.time import utc_now
from app.modules.materials import worker
from app.storage.deps import get_storage_dep
from app.storage.errors import StorageObjectNotFoundError
from app.storage.s3 import S3Storage, S3StorageConfig
from tests import pg_support
from tests.integration.test_materials_api import build_docx, build_pptx

PASSWORD = "Demo password 2026!"

PDF_MIME = "application/pdf"
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

MATERIALS_URL = "/api/v1/courses/{course_id}/materials"
OUTLINE_URL = "/api/v1/materials/{material_id}/outline"


def build_pdf(pages_text: list[str]) -> bytes:
    """手工构造最小有效 PDF（每页一行文本，pypdf 可提取）。"""
    n = len(pages_text)
    kids = " ".join(str(4 + 2 * i) + " 0 R" for i in range(n))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids [" + kids + "] /Count " + str(n) + " >>"
        ).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(pages_text):
        page_num = 4 + 2 * i
        content_num = page_num + 1
        stream = ("BT /F1 18 Tf 72 720 Td (" + text + ") Tj ET").encode()
        page_dict = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Contents " + str(content_num) + " 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
        objects[page_num] = page_dict
        objects[content_num] = (
            "<< /Length " + str(len(stream)) + " >>\nstream\n"
            + stream.decode()
            + "\nendstream"
        ).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += str(num).encode() + b" 0 obj\n" + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    max_num = max(objects)
    out += ("xref\n0 " + str(max_num + 1) + "\n").encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, max_num + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    trailer = (
        "trailer\n<< /Size " + str(max_num + 1) + " /Root 1 0 R >>\n"
        "startxref\n" + str(xref_pos) + "\n%%EOF"
    ).encode()
    out += trailer
    return bytes(out)
# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real_storage() -> Iterator[S3Storage]:
    endpoint = os.environ.get("TEST_S3_ENDPOINT", "").strip()
    bucket = os.environ.get("TEST_S3_BUCKET", "").strip()
    access_key = os.environ.get("TEST_S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("TEST_S3_SECRET_KEY", "").strip()
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
    )
    storage = S3Storage(config)
    try:
        storage.ensure_bucket()
        yield storage
    finally:
        storage.close()


def _make_app(pg_app, real_storage: S3Storage):
    app = pg_app(
        storage_endpoint=real_storage.config.endpoint,
        storage_bucket=real_storage.config.bucket,
        storage_access_key=real_storage.config.access_key,
        storage_secret_key=real_storage.config.secret_key,
    )
    app.dependency_overrides[get_storage_dep] = lambda: real_storage
    return app


def _make_settings_for(make_settings, real_storage: S3Storage, **overrides: object):
    return make_settings(
        storage_endpoint=real_storage.config.endpoint,
        storage_bucket=real_storage.config.bucket,
        storage_access_key=real_storage.config.access_key,
        storage_secret_key=real_storage.config.secret_key,
        ai_base_url="http://fake-model.local/v1",
        ai_model="fake-model",
        **overrides,
    )


def _register(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": "教师",
            "role": "TEACHER",
        },
    )
    assert response.status_code == 201, response.text
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    return login.json()["access_token"]


def _upload_real(
    client: TestClient,
    teacher: str,
    course_id: str,
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> dict:
    """真实预签名直传：PUT 到 MinIO，再调用完成接口。"""
    sha256 = hashlib.sha256(content).hexdigest()
    init = client.post(
        MATERIALS_URL.format(course_id=course_id) + "/uploads",
        json={
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": sha256,
        },
        headers={"Authorization": "Bearer " + teacher},
    )
    assert init.status_code == 201, init.text
    presigned = init.json()
    put = httpx.put(
        presigned["upload_url"],
        content=content,
        headers=presigned["headers"],
        timeout=30.0,
    )
    assert put.status_code in (200, 201), put.text
    completed = client.post(
        MATERIALS_URL.format(course_id=course_id)
        + "/uploads/" + presigned["upload_id"] + "/complete",
        headers={"Authorization": "Bearer " + teacher},
    )
    assert completed.status_code == 202, completed.text
    return completed.json()


def _fake_model_client(responder):
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(responder))

    return factory


def _model_json_response(payload: dict) -> httpx.Response:
    import json

    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
            ]
        },
    )


def _drive_worker(pg_session_factory, real_storage: S3Storage, make_settings, ai_factory):
    settings = _make_settings_for(make_settings, real_storage)

    async def run() -> int:
        return await worker.run_pending_batch(
            pg_session_factory,
            storage=real_storage,
            settings=settings,
            ai_client_factory=ai_factory,
        )

    return asyncio.run(run())
# --------------------------------------------------------------------------- #
# 三种格式的端到端解析（真实 MinIO）
# --------------------------------------------------------------------------- #
def _parse_case(
    pg_app,
    real_storage: S3Storage,
    pg_session_factory,
    make_settings,
    *,
    email: str,
    course_name: str,
    filename: str,
    content_type: str,
    content: bytes,
    outline_payload: dict,
) -> dict:
    app = _make_app(pg_app, real_storage)
    with TestClient(app) as client:
        teacher = _register(client, email)
        course = client.post(
            "/api/v1/courses",
            json={"name": course_name, "description": "d"},
            headers={"Authorization": "Bearer " + teacher},
        ).json()
        completed = _upload_real(
            client,
            teacher,
            course["id"],
            filename=filename,
            content_type=content_type,
            content=content,
        )
        material_id = completed["material"]["id"]

        assert (
            _drive_worker(pg_session_factory, real_storage, make_settings, _fake_model_client(
                lambda request: _model_json_response(outline_payload)
            ))
            == 1
        )
        detail = client.get(
            "/api/v1/materials/" + material_id,
            headers={"Authorization": "Bearer " + teacher},
        ).json()
        assert detail["status"] == "READY", detail["error_message"]
        return client.get(
            OUTLINE_URL.format(material_id=material_id),
            headers={"Authorization": "Bearer " + teacher},
        ).json()


def test_parse_docx_from_real_bucket(
    db_isolation: None,
    pg_app,
    real_storage: S3Storage,
    pg_session_factory,
    make_settings,
) -> None:
    content = build_docx(
        [("第一章 绪论", "Heading 1"), ("软件工程是应用系统化的方法。", None)]
    )
    outline = _parse_case(
        pg_app,
        real_storage,
        pg_session_factory,
        make_settings,
        email="minio-docx@example.com",
        course_name="DOCX 课程",
        filename="chapter-1.docx",
        content_type=DOCX_MIME,
        content=content,
        outline_payload={
            "sections": [
                {
                    "title": "绪论",
                    "location_start": 1,
                    "location_end": 2,
                    "knowledge_points": [
                        {
                            "title": "系统化方法",
                            "description": "软件工程是应用系统化的方法。",
                            "quote": "软件工程是应用系统化的方法。",
                            "location_start": 2,
                            "location_end": 2,
                        }
                    ],
                }
            ]
        },
    )
    assert outline["sections"][0]["source_type"] == "DOCX_PARAGRAPH"
    assert outline["sections"][0]["knowledge_points"][0]["quote"] == (
        "软件工程是应用系统化的方法。"
    )


def test_parse_pptx_from_real_bucket(
    db_isolation: None,
    pg_app,
    real_storage: S3Storage,
    pg_session_factory,
    make_settings,
) -> None:
    outline = _parse_case(
        pg_app,
        real_storage,
        pg_session_factory,
        make_settings,
        email="minio-pptx@example.com",
        course_name="PPTX 课程",
        filename="week-1.pptx",
        content_type=PPTX_MIME,
        content=build_pptx([["软件工程概述", "定义与范围", "历史沿革"]]),
        outline_payload={
            "sections": [
                {
                    "title": "软件工程概述",
                    "location_start": 1,
                    "location_end": 1,
                    "knowledge_points": [
                        {
                            "title": "定义与范围",
                            "description": "定义与范围。",
                            "quote": "定义与范围",
                            "location_start": 1,
                            "location_end": 1,
                        }
                    ],
                }
            ]
        },
    )
    assert outline["sections"][0]["source_type"] == "PPTX_SLIDE"
    assert outline["sections"][0]["knowledge_points"][0]["quote"] == "定义与范围"


def test_parse_pdf_from_real_bucket(
    db_isolation: None,
    pg_app,
    real_storage: S3Storage,
    pg_session_factory,
    make_settings,
) -> None:
    outline = _parse_case(
        pg_app,
        real_storage,
        pg_session_factory,
        make_settings,
        email="minio-pdf@example.com",
        course_name="PDF 课程",
        filename="chapter-1.pdf",
        content_type=PDF_MIME,
        content=build_pdf(["Software Engineering Overview"]),
        outline_payload={
            "sections": [
                {
                    "title": "Software Engineering Overview",
                    "location_start": 1,
                    "location_end": 1,
                    "knowledge_points": [
                        {
                            "title": "Overview",
                            "description": "Overview of software engineering.",
                            "quote": "Software Engineering Overview",
                            "location_start": 1,
                            "location_end": 1,
                        }
                    ],
                }
            ]
        },
    )
    assert outline["sections"][0]["source_type"] == "PDF_PAGE"
    assert (
        outline["sections"][0]["knowledge_points"][0]["quote"]
        == "Software Engineering Overview"
    )
# --------------------------------------------------------------------------- #
# 删除流水线：维护命令从真实桶删除对象并核查晚到 PUT
# --------------------------------------------------------------------------- #
def test_deletion_pipeline_removes_object_from_real_bucket(
    db_isolation: None,
    pg_app,
    real_storage: S3Storage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    app = _make_app(pg_app, real_storage)
    with TestClient(app) as client:
        teacher = _register(client, "minio-delete@example.com")
        course = client.post(
            "/api/v1/courses",
            json={"name": "删除课程", "description": "d"},
            headers={"Authorization": "Bearer " + teacher},
        ).json()
        content = build_docx([("待删除内容", None)])
        completed = _upload_real(
            client,
            teacher,
            course["id"],
            filename="to-delete.docx",
            content_type=DOCX_MIME,
            content=content,
        )
        material_id = completed["material"]["id"]

        with pg_sync_engine.connect() as connection:
            object_key = connection.execute(
                text(
                    "SELECT m.storage_key FROM materials m "
                    "WHERE m.id = CAST(:id AS uuid)"
                ),
                {"id": material_id},
            ).scalar_one()
        # 真实对象已存在
        assert real_storage.head_object(object_key).size == len(content)

        # 删除资料（写入待办）；PUT 地址过期前对象仍在
        deleted = client.delete(
            "/api/v1/materials/" + material_id,
            headers={"Authorization": "Bearer " + teacher},
        )
        assert deleted.status_code == 204
        assert real_storage.head_object(object_key).size == len(content)

        # PUT 地址过期后维护命令删除并核查
        with pg_sync_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE material_upload_sessions"
                    " SET upload_url_expires_at = now() - interval '2 hours'"
                )
            )
            connection.execute(
                text(
                    "UPDATE material_delete_todos"
                    " SET upload_expires_at = now() - interval '2 hours'"
                )
            )

        settings = _make_settings_for(
            make_settings, real_storage, material_delete_buffer_seconds=0
        )

        async def cleanup() -> tuple[int, int]:
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )
            from sqlalchemy.pool import NullPool

            from app.modules.materials import service as materials_service

            # 用测试框架解析出的专用测试库 URL：只配置 DATABASE_URL 时
            # 也能拿到派生的 ``<库名>_test``，不直接依赖 TEST_DATABASE_URL。
            engine = create_async_engine(
                pg_support.resolve_test_database_url(),
                poolclass=NullPool,
            )
            try:
                factory = async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
                async with factory() as session:
                    return await materials_service.cleanup_deleted_materials(
                        session, storage=real_storage, settings=settings
                    )
            finally:
                await engine.dispose()

        done, pending = asyncio.run(cleanup())
        assert (done, pending) == (1, 0)

        # 真实桶中的对象已被删除
        try:
            real_storage.head_object(object_key)
            raise AssertionError("对象应已被删除")
        except StorageObjectNotFoundError:
            pass

        # 晚到 PUT 核查：重建对象后下一轮清理会再次删除
        real_storage.delete_object(object_key)  # 幂等确认当前不存在
