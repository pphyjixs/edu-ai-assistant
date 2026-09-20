"""过期上传会话清理集成测试（真实 PostgreSQL + 内存对象存储假实现）。

对应契约 4.6 的过期清理与用户任务的验收：

- 超过确认窗口仍未完成的会话：清理删除其孤立对象并标记过期；
- 已完成会话及其资料、对象**绝不删除**；
- 清理可重复执行（幂等）：第二次运行不再重复处理；
- 与完成请求并发时，不会误删已确认对象（``FOR UPDATE SKIP LOCKED`` 互斥）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.time import utc_now
from app.storage import S3Storage, S3StorageConfig, StorageObjectNotFoundError
from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage

PASSWORD = "Demo password 2026!"

UPLOADS_URL = "/api/v1/courses/{course_id}/materials/uploads"
COMPLETE_URL = "/api/v1/courses/{course_id}/materials/uploads/{upload_id}/complete"

BODY = b"%PDF-1.7 cleanup test\n"
SHA256_HEX = hashlib.sha256(BODY).hexdigest()
PDF_MIME = "application/pdf"


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> AsyncIterator[httpx.AsyncClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as async_http_client:
        yield async_http_client


def _register(client, email: str, role: str, *, name: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": name,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text


def _login(client, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _teacher_token(client, email: str = "teacher@example.com") -> str:
    _register(client, email, "TEACHER", name="张老师")
    return _login(client, email)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_course(client, token: str) -> dict:
    response = client.post(
        "/api/v1/courses",
        json={"name": "清理测试课程", "description": ""},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _init_upload(client, token: str, course_id: str) -> dict:
    response = client.post(
        UPLOADS_URL.format(course_id=course_id),
        json={
            "filename": "chapter-1.pdf",
            "content_type": PDF_MIME,
            "size": len(BODY),
            "sha256": SHA256_HEX,
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _simulate_upload(fake: FakeStorage, upload_url: str) -> str:
    for key, url in fake.presigned_urls.items():
        if url == upload_url:
            fake.store_object(
                key, size=len(BODY), content_type=PDF_MIME, sha256_hex=SHA256_HEX
            )
            return key
    raise AssertionError("未找到对象键")


def _complete_upload(client, token: str, course_id: str, upload_id: str):
    return client.post(
        COMPLETE_URL.format(course_id=course_id, upload_id=upload_id),
        headers=_auth(token),
    )


def _expire_session(engine: Engine, upload_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE material_upload_sessions"
                " SET confirm_deadline_at = now() - interval '1 minute'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": upload_id},
        )


def _session_row(engine: Engine, upload_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT completed_material_id, expired_at"
                    " FROM material_upload_sessions WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        )


async def _run_cleanup(fake_storage: FakeStorage, pg_session_factory) -> int:
    """直接调用 service 的清理逻辑（等价于脚本的清理路径）。

    使用 conftest 提供的测试库会话工厂，确保清理作用在测试库而非开发库上。
    """
    from app.modules.materials import service as materials_service

    async with pg_session_factory() as session:
        return await materials_service.cleanup_expired_uploads(
            session, storage=fake_storage
        )


async def test_cleanup_removes_orphan_object_and_marks_expired(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine, pg_session_factory
) -> None:
    """过期未完成的会话：清理删除孤立对象并标记过期。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"])
    object_key = _simulate_upload(fake_storage, init["upload_url"])
    _expire_session(pg_sync_engine, init["upload_id"])

    # 清理前：对象还在，会话未标记过期
    assert object_key in fake_storage.objects
    assert _session_row(pg_sync_engine, init["upload_id"])["expired_at"] is None

    cleaned = await _run_cleanup(fake_storage, pg_session_factory)

    assert cleaned == 1
    # 孤立对象已删除
    assert object_key not in fake_storage.objects
    # 会话已标记过期
    row = _session_row(pg_sync_engine, init["upload_id"])
    assert row["expired_at"] is not None
    assert row["completed_material_id"] is None


async def test_cleanup_is_idempotent(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine, pg_session_factory
) -> None:
    """清理可重复执行：第二次运行不重复处理已标记的会话。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"])
    _simulate_upload(fake_storage, init["upload_url"])
    _expire_session(pg_sync_engine, init["upload_id"])

    first = await _run_cleanup(fake_storage, pg_session_factory)
    second = await _run_cleanup(fake_storage, pg_session_factory)

    assert first == 1
    assert second == 0  # 已标记过期，不再重复处理


async def test_cleanup_preserves_completed_material(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine, pg_session_factory
) -> None:
    """已完成的会话及其资料、对象不得被清理。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"])
    object_key = _simulate_upload(fake_storage, init["upload_url"])
    completed = _complete_upload(client, token, course["id"], init["upload_id"])
    assert completed.status_code == 202, completed.text

    # 即使把已完成会话的确认窗口「人为拨过期」，清理也不得动它
    _expire_session(pg_sync_engine, init["upload_id"])

    cleaned = await _run_cleanup(fake_storage, pg_session_factory)

    assert cleaned == 0
    # 已完成资料的对象仍在
    assert object_key in fake_storage.objects
    # 资料与任务仍在
    assert _count(pg_sync_engine, "materials") == 1
    assert _count(pg_sync_engine, "jobs") == 1
    # 会话未被标记过期（已完成的会话不属于清理范围）
    row = _session_row(pg_sync_engine, init["upload_id"])
    assert row["completed_material_id"] is not None
    assert row["expired_at"] is None


async def test_cleanup_skips_when_storage_unavailable(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine, pg_session_factory
) -> None:
    """存储不可用时清理跳过该会话，不误标过期（下次清理再处理）。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"])
    _simulate_upload(fake_storage, init["upload_url"])
    _expire_session(pg_sync_engine, init["upload_id"])

    # 存储不可用：删除对象会抛 StorageUnavailableError
    fake_storage.as_unavailable("connection")

    cleaned = await _run_cleanup(fake_storage, pg_session_factory)

    # 本次不标记过期
    assert cleaned == 0
    row = _session_row(pg_sync_engine, init["upload_id"])
    assert row["expired_at"] is None

    # 存储恢复后再次清理，才真正处理
    fake_storage.as_available()
    cleaned = await _run_cleanup(fake_storage, pg_session_factory)
    assert cleaned == 1
    assert _session_row(pg_sync_engine, init["upload_id"])["expired_at"] is not None


async def test_cleanup_concurrent_with_complete_does_not_delete_confirmed_object(
    async_client: httpx.AsyncClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完成请求持有会话行锁时，清理跳过该行；完成后对象仍存在。"""
    from app.modules.materials import service as materials_service
    from app.modules.materials import repository as materials_repo

    # 注册教师、创建课程、初始化并模拟直传
    await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": "cleanup-race@example.com",
            "password": PASSWORD,
            "display_name": "张老师",
            "role": "TEACHER",
        },
    )
    token = (
        await async_client.post(
            "/api/v1/auth/login",
            json={"email": "cleanup-race@example.com", "password": PASSWORD},
        )
    ).json()["access_token"]
    course = (
        await async_client.post(
            "/api/v1/courses",
            json={"name": "并发清理课程", "description": ""},
            headers=_auth(token),
        )
    ).json()
    init = (
        await async_client.post(
            UPLOADS_URL.format(course_id=course["id"]),
            json={
                "filename": "chapter-1.pdf",
                "content_type": PDF_MIME,
                "size": len(BODY),
                "sha256": SHA256_HEX,
            },
            headers=_auth(token),
        )
    ).json()
    object_key = _simulate_upload(fake_storage, init["upload_url"])

    async def do_cleanup() -> int:
        async with pg_session_factory() as session:
            return await materials_service.cleanup_expired_uploads(
                # 模拟截止时间刚到：清理认为会话过期，而已开始的完成请求
                # 仍按其进入临界区时的真实时钟检查确认窗口。
                session, storage=fake_storage, now=utc_now() + timedelta(days=2)
            )

    async def do_complete():
        return await async_client.post(
            COMPLETE_URL.format(course_id=course["id"], upload_id=init["upload_id"]),
            headers=_auth(token),
        )

    # 确定性地暂停在完成请求已取得 FOR UPDATE 行锁之后，
    # 让清理请求真正与它同时争用同一个上传会话。
    locked = asyncio.Event()
    resume_complete = asyncio.Event()
    original_get_for_update = materials_repo.get_upload_session_for_update

    async def pause_after_lock(session, upload_id):
        upload = await original_get_for_update(session, upload_id)
        locked.set()
        await resume_complete.wait()
        return upload

    monkeypatch.setattr(materials_repo, "get_upload_session_for_update", pause_after_lock)
    complete_task = asyncio.create_task(do_complete())
    try:
        await asyncio.wait_for(locked.wait(), timeout=10)
        # SKIP LOCKED 应立即返回，不应等待完成请求释放行锁。
        cleaned = await asyncio.wait_for(do_cleanup(), timeout=10)
        assert cleaned == 0
        assert object_key in fake_storage.objects
    finally:
        resume_complete.set()

    complete_resp = await asyncio.wait_for(complete_task, timeout=10)
    assert complete_resp.status_code == 202, complete_resp.text

    # 即使后来已超过截止时间，已确认对象也不属于清理范围。
    _expire_session(pg_sync_engine, init["upload_id"])
    cleaned = await do_cleanup()
    assert cleaned == 0
    assert object_key in fake_storage.objects
    assert _count(pg_sync_engine, "materials") == 1


def test_cleanup_command_against_test_database_and_storage(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_test_url: str,
) -> None:
    """维护命令在隔离测试库与测试桶中真实删除对象，重复运行无副作用。"""
    required = (
        "TEST_S3_ENDPOINT",
        "TEST_S3_BUCKET",
        "TEST_S3_ACCESS_KEY",
        "TEST_S3_SECRET_KEY",
    )
    if any(not os.environ.get(name) for name in required):
        pytest.skip("未配置 TEST_S3_*，无法验证清理命令的真实存储删除")

    config = S3StorageConfig(
        endpoint=os.environ["TEST_S3_ENDPOINT"].rstrip("/"),
        bucket=os.environ["TEST_S3_BUCKET"],
        access_key=os.environ["TEST_S3_ACCESS_KEY"],
        secret_key=os.environ["TEST_S3_SECRET_KEY"],
        region=os.environ.get("TEST_S3_REGION", "us-east-1"),
        path_style=os.environ.get("TEST_S3_PATH_STYLE", "true").lower() != "false",
    )
    storage = S3Storage(config)
    try:
        storage.ensure_bucket()
        token = _teacher_token(client)
        course = _create_course(client, token)
        init = _init_upload(client, token, course["id"])
        object_key = _simulate_upload(fake_storage, init["upload_url"])
        upload = storage.create_presigned_put(
            object_key, content_type=PDF_MIME, sha256_hex=SHA256_HEX
        )
        with httpx.Client(timeout=15.0) as direct_client:
            response = direct_client.put(upload.url, headers=upload.headers, content=BODY)
        assert response.status_code in (200, 201), response.status_code
        _expire_session(pg_sync_engine, init["upload_id"])

        backend_dir = Path(__file__).resolve().parents[2]
        command_env = {
            **os.environ,
            "DATABASE_URL": pg_test_url,
            "STORAGE_ENDPOINT": config.endpoint,
            "STORAGE_BUCKET": config.bucket,
            "STORAGE_ACCESS_KEY": config.access_key,
            "STORAGE_SECRET_KEY": config.secret_key,
            "STORAGE_REGION": config.region,
            "STORAGE_PATH_STYLE": str(config.path_style).lower(),
        }
        command = [sys.executable, str(backend_dir / "scripts" / "cleanup_expired_uploads.py")]
        first = subprocess.run(
            command, cwd=backend_dir, env=command_env, capture_output=True, text=True,
            timeout=30, check=False,
        )
        assert first.returncode == 0
        assert _session_row(pg_sync_engine, init["upload_id"])["expired_at"] is not None
        with pytest.raises(StorageObjectNotFoundError):
            storage.head_object(object_key)

        second = subprocess.run(
            command, cwd=backend_dir, env=command_env, capture_output=True, text=True,
            timeout=30, check=False,
        )
        assert second.returncode == 0
        assert "0 条" in second.stdout
    finally:
        storage.close()
