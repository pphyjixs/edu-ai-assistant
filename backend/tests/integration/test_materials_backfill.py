"""检索片段回填命令的集成测试（专用测试库）。

对应 ``docs/api-contract.md`` 6.1 的运维配套：早于片段功能解析完成的
``READY`` 资料没有片段，需要由 ``scripts/backfill_material_chunks.py``
补建。本文件验证：

- 缺片段的 ``READY`` 资料被正确回填；
- 重复执行结果一致（不累积、内容一致）；
- 回填期间被删除或状态变化的资料不会留下可检索片段；
- 读取对象失败时输出安全摘要，且**保留资料原状态**供重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.modules.materials import backfill
from tests.integration.test_chat_api import (  # noqa: F401 - 复用构造
    _create_course_with_material,
    _make_chat_client,
    make_answering_factory,
)
from tests.integration.test_materials_api import (
    DOCX_MIME,
    _auth,
    _fake_model_client,
    build_docx,
)
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    """注入假模型的应用客户端（本模块自定义，避免跨模块解析夹具依赖）。"""
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, make_answering_factory()
    ) as test_client:
        yield test_client


def _run_backfill(pg_session_factory, fake_storage, make_settings, **kwargs):
    """同步包装：在测试库上跑一次回填（与维护命令同一入口）。"""
    settings = make_settings()

    async def run():
        async with pg_session_factory() as session:
            return await backfill.backfill_material_chunks(
                session, storage=fake_storage, settings=settings, **kwargs
            )

    return asyncio.run(run())


def _chunk_rows(pg_sync_engine, material_id: str) -> list[dict]:
    with pg_sync_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    'SELECT "order", content, location_start, location_end'
                    " FROM material_chunks WHERE material_id = CAST(:id AS uuid)"
                    ' ORDER BY "order"'
                ),
                {"id": material_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _drop_chunks(pg_sync_engine, material_id: str) -> None:
    """模拟旧数据：资料是 READY 但没有片段（片段功能上线前解析的）。"""
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM material_chunks WHERE material_id = CAST(:id AS uuid)"
            ),
            {"id": material_id},
        )


def _teacher_id(pg_sync_engine, email: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT id FROM users WHERE email_normalized = :email"),
            {"email": email.lower()},
        ).scalar_one()


def _insert_ready_material(
    pg_sync_engine,
    fake_storage,
    *,
    course_id: str,
    teacher_id: str,
    index: int,
    content: bytes,
) -> str:
    """直接插入一条 READY 且**没有片段**的资料（模拟片段功能上线前的数据）。

    对象同时写入假存储；``index`` 用来递增 ``created_at``，保证游标顺序稳定。
    """
    material_id = uuid.uuid4()
    upload_id = uuid.uuid4()
    object_key = f"backfill/{index}.docx"
    filename = f"legacy-{index}.docx"
    sha256_hex = hashlib.sha256(content).hexdigest()
    fake_storage.store_object(
        object_key,
        size=len(content),
        content_type=DOCX_MIME,
        sha256_hex=sha256_hex,
        content=content,
    )

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO material_upload_sessions"
                " (id, course_id, teacher_id, object_key, filename, content_type,"
                "  size, sha256, upload_url_expires_at, confirm_deadline_at,"
                "  created_at, updated_at)"
                " VALUES (CAST(:id AS uuid), CAST(:course_id AS uuid),"
                "  CAST(:teacher_id AS uuid), :object_key, :filename, :content_type,"
                "  :size, :sha256, now(), now() + interval '1 day',"
                "  now() + CAST(:offset_ms AS int) * interval '1 millisecond',"
                "  now() + CAST(:offset_ms AS int) * interval '1 millisecond')"
            ),
            {
                "id": str(upload_id),
                "course_id": course_id,
                "teacher_id": teacher_id,
                "object_key": object_key,
                "filename": filename,
                "content_type": DOCX_MIME,
                "size": len(content),
                "sha256": sha256_hex,
                "offset_ms": index,
            },
        )
        connection.execute(
            text(
                "INSERT INTO materials"
                " (id, course_id, upload_id, filename, content_type, size, sha256,"
                "  storage_key, status, uploaded_by, error_message, deleted_at,"
                "  created_at, updated_at)"
                " VALUES (CAST(:id AS uuid), CAST(:course_id AS uuid),"
                "  CAST(:upload_id AS uuid), :filename, :content_type, :size, :sha256,"
                "  :object_key, 'READY', CAST(:teacher_id AS uuid), NULL, NULL,"
                "  now() + CAST(:offset_ms AS int) * interval '1 millisecond',"
                "  now() + CAST(:offset_ms AS int) * interval '1 millisecond')"
            ),
            {
                "id": str(material_id),
                "course_id": course_id,
                "upload_id": str(upload_id),
                "filename": filename,
                "content_type": DOCX_MIME,
                "size": len(content),
                "sha256": sha256_hex,
                "object_key": object_key,
                "teacher_id": teacher_id,
                "offset_ms": index,
            },
        )
    return str(material_id)


def _set_status(pg_sync_engine, material_id: str, status: str) -> None:
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE materials SET status = CAST(:status AS material_status)"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": material_id, "status": status},
        )


# --------------------------------------------------------------------------- #
# 回填与幂等
# --------------------------------------------------------------------------- #
def test_backfill_creates_chunks_for_legacy_ready_material(
    client,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """没有片段的 READY 资料被补建片段，且内容可检索。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)
    assert _chunk_rows(pg_sync_engine, material_id) == []

    report = _run_backfill(pg_session_factory, fake_storage, make_settings)

    assert report.filled == 1
    assert report.failed_count == 0
    rows = _chunk_rows(pg_sync_engine, material_id)
    assert rows, "回填后必须建立片段索引"
    assert [row["order"] for row in rows] == list(range(1, len(rows) + 1))
    joined = "\n".join(row["content"] for row in rows)
    assert "软件工程是应用系统化的方法。" in joined


def test_backfill_is_idempotent(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """重复执行结果一致：已有片段的资料被跳过，片段不累积。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-idem@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)

    first = _run_backfill(pg_session_factory, fake_storage, make_settings)
    rows_after_first = _chunk_rows(pg_sync_engine, material_id)
    second = _run_backfill(pg_session_factory, fake_storage, make_settings)
    rows_after_second = _chunk_rows(pg_sync_engine, material_id)

    assert first.filled == 1
    assert second.filled == 0, "第二次运行不应再回填已有片段的资料"
    assert second.failed_count == 0
    assert rows_after_first == rows_after_second


def test_backfill_force_rewrites_without_accumulating(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """``--force`` 重做已有片段：全量重写，数量与内容保持一致。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-force@example.com",
    )
    before = _chunk_rows(pg_sync_engine, material_id)

    report = _run_backfill(
        pg_session_factory, fake_storage, make_settings, force=True
    )

    assert report.filled == 1
    after = _chunk_rows(pg_sync_engine, material_id)
    assert len(after) == len(before)
    assert [row["content"] for row in after] == [row["content"] for row in before]


# --------------------------------------------------------------------------- #
# 边界：回填期间资料被删除或状态变化
# --------------------------------------------------------------------------- #
def test_backfill_skips_material_that_is_not_ready_anymore(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """回填前状态已变为 FAILED 的资料不再是候选，不会留下片段。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-status@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)
    _set_status(pg_sync_engine, material_id, "FAILED")

    report = _run_backfill(pg_session_factory, fake_storage, make_settings)

    assert report.filled == 0
    assert _chunk_rows(pg_sync_engine, material_id) == []


def test_replace_chunks_recheck_blocks_deleted_material(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """提交前复查：资料在写入前被删除时，一行片段都不会写入。"""
    _, teacher, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-recheck@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)
    deleted = client.delete(
        f"/api/v1/materials/{material_id}", headers=_auth(teacher)
    )
    assert deleted.status_code == 204

    from app.modules.materials import extraction

    chunks = [
        extraction.RetrievalChunk(
            index=1, location_start=1, location_end=1, content="回填内容"
        )
    ]

    async def write() -> bool:
        async with pg_session_factory() as session:
            from app.core.time import utc_now

            return await backfill._replace_chunks(
                session,
                material_id=uuid.UUID(material_id),
                chunks=chunks,
                now=utc_now(),
            )

    assert asyncio.run(write()) is False
    assert _chunk_rows(pg_sync_engine, material_id) == []


# --------------------------------------------------------------------------- #
# 失败：安全摘要 + 保留原状态供重试
# --------------------------------------------------------------------------- #
def _storage_key(pg_sync_engine, material_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT storage_key FROM materials WHERE id = CAST(:id AS uuid)"),
            {"id": material_id},
        ).scalar_one()


def _material_status(pg_sync_engine, material_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM materials WHERE id = CAST(:id AS uuid)"),
            {"id": material_id},
        ).scalar_one()


def test_backfill_reports_safe_failure_and_keeps_status(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """对象缺失时输出安全摘要；资料仍是 READY，下次运行继续重试。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-missing@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)
    storage_key = _storage_key(pg_sync_engine, material_id)
    fake_storage.objects.pop(storage_key)  # 模拟对象已丢失

    report = _run_backfill(pg_session_factory, fake_storage, make_settings)

    assert report.filled == 0
    assert report.failed_count == 1
    failure = report.failed[0]
    assert failure.material_id == uuid.UUID(material_id)
    assert failure.reason  # 有可安全展示的摘要
    assert storage_key not in failure.reason  # 不回显对象键
    # 原状态保留：资料仍可被下一次运行重试
    assert _material_status(pg_sync_engine, material_id) == "READY"
    assert _chunk_rows(pg_sync_engine, material_id) == []


def test_backfill_can_target_single_material(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """``--material-id`` 只处理指定资料（排障入口）。"""
    _, _, first_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-target@example.com",
    )
    _drop_chunks(pg_sync_engine, first_id)

    report = _run_backfill(
        pg_session_factory,
        fake_storage,
        make_settings,
        material_id=uuid.UUID(first_id),
    )

    assert report.filled == 1
    assert _chunk_rows(pg_sync_engine, first_id)


# --------------------------------------------------------------------------- #
# 跨批次遍历：批量大小不是总上限
# --------------------------------------------------------------------------- #
def test_backfill_scans_all_pages_with_failure_in_first_page(
    client,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """最早一批含持续失败项时，后续批次仍会被处理（游标推进、失败不阻断）。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-pages@example.com",
    )
    teacher_id = _teacher_id(pg_sync_engine, "backfill-pages@example.com")
    content = build_docx([("回填跨批次的标识词甲乙丙丁", None)])

    total = 105
    material_ids = [
        _insert_ready_material(
            pg_sync_engine,
            fake_storage,
            course_id=course_id,
            teacher_id=teacher_id,
            index=index,
            content=content,
        )
        for index in range(total)
    ]
    # 最早的一条对象缺失：持续失败项，位于第一页
    first_key = _storage_key(pg_sync_engine, material_ids[0])
    fake_storage.objects.pop(first_key)

    report = _run_backfill(
        pg_session_factory, fake_storage, make_settings, batch_size=50
    )

    assert report.filled == total - 1, "跨页的后续资料同样必须被回填"
    assert report.failed_count == 1
    assert report.skipped == 0
    assert report.ok is False, "存在失败资料时不得报告成功"
    assert report.failed[0].material_id == uuid.UUID(material_ids[0])
    assert first_key not in report.failed[0].reason

    # 最后一页的资料也已建立片段索引
    assert _chunk_rows(pg_sync_engine, material_ids[-1])
    # 第一页其余资料同样完成
    assert _chunk_rows(pg_sync_engine, material_ids[1])

    # 重复运行：只重试仍缺片段的那一条（其余资料被跳过，不重复处理）
    second = _run_backfill(
        pg_session_factory, fake_storage, make_settings, batch_size=50
    )
    assert second.filled == 0
    assert second.failed_count == 1
    assert second.failed[0].material_id == uuid.UUID(material_ids[0])


def test_backfill_force_visits_every_candidate_once(
    client,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """`--force` 在一次运行中覆盖全部候选，每条只访问一次、不累积片段。"""
    course_id, teacher, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-force-pages@example.com",
    )
    teacher_id = _teacher_id(pg_sync_engine, "backfill-force-pages@example.com")
    content = build_docx([("强制重写的标识词戊己庚辛", None)])
    extra_ids = [
        _insert_ready_material(
            pg_sync_engine,
            fake_storage,
            course_id=course_id,
            teacher_id=teacher_id,
            index=index,
            content=content,
        )
        for index in range(3)
    ]

    uploaded_before = len(_chunk_rows(pg_sync_engine, material_id))
    report = _run_backfill(
        pg_session_factory, fake_storage, make_settings, force=True, batch_size=2
    )

    # 4 条资料（真实上传 1 条 + 直插 3 条）都被访问一次
    assert report.filled == 4
    assert report.failed_count == 0
    assert report.skipped == 0
    assert report.ok is True
    # 已有片段的资料：force 重写后数量不累积
    assert len(_chunk_rows(pg_sync_engine, material_id)) == uploaded_before
    # 原本缺片段的资料：force 模式同样补齐
    for mid in extra_ids:
        assert _chunk_rows(pg_sync_engine, mid), "force 模式下每条候选都应有片段"


def test_backfill_rejects_non_positive_batch_size(
    client,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """``batch_size <= 0`` 必须报错，而不是漏掉候选后报告成功。"""
    _, _, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="backfill-bad-batch@example.com",
    )
    _drop_chunks(pg_sync_engine, material_id)

    for bad_batch in (0, -1):
        with pytest.raises(ValueError):
            _run_backfill(
                pg_session_factory, fake_storage, make_settings, batch_size=bad_batch
            )

    # 非法参数不产生任何副作用：资料仍缺片段，等待一次合法运行
    assert _chunk_rows(pg_sync_engine, material_id) == []

    # 合法参数仍然能正常回填（确认上面失败不是环境问题）
    report = _run_backfill(pg_session_factory, fake_storage, make_settings)
    assert report.filled == 1
    assert _chunk_rows(pg_sync_engine, material_id)
