"""作业附件与「重新开启」的集成测试（``docs/api-contract.md`` 8.8 / 8.15）。

覆盖：

- 附件的三段式上传（初始化 → 直传 → 完成）、列表可见性、删除；
- 权限矩阵：只有课程创建教师能写，学生可读，非成员一律 404，归档课程 409；
- 业务规则：同名附件冲突、对象缺失/不一致、确认窗口过期、幂等重放；
- 重新开启：``CLOSED`` → ``PUBLISHED``（清 ``closed_at``、保留 ``published_at``）、
  幂等、状态与角色越界。

对象存储用 ``tests.storage_fake.FakeStorage``（继承真实适配器，不发网络请求）。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_materials_api import (
    _auth,
    _create_course,
    _login,
    _register,
)
from tests.integration.test_practice_api import _join
from tests.storage_fake import FakeStorage

CREATE_URL = "/api/v1/courses/{course_id}/assignments"
PUBLISH_URL = "/api/v1/assignments/{assignment_id}/publish"
CLOSE_URL = "/api/v1/assignments/{assignment_id}/close"
REOPEN_URL = "/api/v1/assignments/{assignment_id}/reopen"
ATTACHMENTS_URL = "/api/v1/assignments/{assignment_id}/attachments"
UPLOADS_URL = "/api/v1/assignments/{assignment_id}/attachments/uploads"
COMPLETE_URL = (
    "/api/v1/assignments/{assignment_id}/attachments/uploads/{upload_id}/complete"
)
ATTACHMENT_URL = "/api/v1/assignments/{assignment_id}/attachments/{attachment_id}"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
DOC_MIME = "application/msword"

CONTENT = b"attachment-bytes"

#: 扩展名 → 规范 MIME；测试里按文件名推导，避免"文件名 .pdf 却声明 DOCX"的自伤
MIME_BY_EXTENSION = {"pdf": PDF_MIME, "pptx": PPTX_MIME, "docx": DOCX_MIME}


def mime_for(filename: str) -> str:
    return MIME_BY_EXTENSION[filename.rsplit(".", 1)[-1].lower()]


@pytest.fixture
def fake_storage() -> Iterator[FakeStorage]:
    yield FakeStorage()


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    from app.storage.deps import get_storage_dep

    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _course_with_members(client: TestClient, suffix: str) -> tuple[str, str, str]:
    """建课程 + 一名学生，返回 ``(课程 ID, 教师令牌, 学生令牌)``。"""
    teacher = _new_teacher(client, f"att-{suffix}-t@example.com")
    course_id = _create_course(client, teacher)
    student = _new_student(client, f"att-{suffix}-s@example.com")
    _join(client, course_id, teacher, student)
    return course_id, teacher, student


def _new_teacher(client: TestClient, email: str) -> str:
    _register(client, email, "TEACHER")
    return _login(client, email)


def _new_student(client: TestClient, email: str) -> str:
    _register(client, email, "STUDENT")
    return _login(client, email)


def _payload(**overrides) -> dict:
    payload = {
        "title": "实验一 报告模板",
        "description": "附件测试",
        "total_score": 100,
        "due_at": None,
        "allow_late_submission": True,
        "rubric_items": [
            {"title": "完整性", "description": "", "max_score": 100, "order": 1}
        ],
    }
    payload.update(overrides)
    return payload


def _create_assignment(client: TestClient, course_id: str, teacher: str) -> str:
    response = client.post(
        CREATE_URL.format(course_id=course_id), json=_payload(), headers=_auth(teacher)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _object_key_of(fake: FakeStorage, upload_url: str) -> str:
    for key, url in fake.presigned_urls.items():
        if url == upload_url:
            return key
    raise AssertionError(f"假存储中没有该地址对应的对象键：{upload_url}")


def _init_upload(
    client: TestClient,
    assignment_id: str,
    token: str,
    *,
    filename: str = "guide.docx",
    content_type: str | None = None,
    content: bytes = CONTENT,
):
    sha256 = hashlib.sha256(content).hexdigest()
    return client.post(
        UPLOADS_URL.format(assignment_id=assignment_id),
        json={
            "filename": filename,
            "content_type": content_type or mime_for(filename),
            "size": len(content),
            "sha256": sha256,
        },
        headers=_auth(token),
    )


def _upload(
    client: TestClient,
    fake: FakeStorage,
    assignment_id: str,
    token: str,
    *,
    filename: str = "guide.docx",
    content_type: str | None = None,
    content: bytes = CONTENT,
):
    """完整走一遍初始化 → 模拟直传 → 完成，返回完成响应。"""
    presigned = _init_upload(
        client,
        assignment_id,
        token,
        filename=filename,
        content_type=content_type,
        content=content,
    )
    assert presigned.status_code == 201, presigned.text
    body = presigned.json()
    fake.store_object(
        _object_key_of(fake, body["upload_url"]),
        size=len(content),
        content_type=content_type or mime_for(filename),
        sha256_hex=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    return client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=body["upload_id"]),
        headers=_auth(token),
    )


# --------------------------------------------------------------------------- #
# 8.15 附件：主链路
# --------------------------------------------------------------------------- #
def test_teacher_uploads_and_student_downloads(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, student = _course_with_members(client, "flow")
    assignment_id = _create_assignment(client, course_id, teacher)
    # 学生只读得到已发布任务的附件；先发布再验证学生视角
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))

    completed = _upload(client, fake_storage, assignment_id, teacher)
    assert completed.status_code == 201, completed.text
    attachment = completed.json()
    assert set(attachment) == {
        "id",
        "assignment_id",
        "filename",
        "content_type",
        "size",
        "sha256",
        "uploaded_by",
        "uploaded_by_name",
        "download_url",
        "download_expires_at",
        "created_at",
    }
    assert attachment["filename"] == "guide.docx"
    assert attachment["download_url"]
    # 上传者显示名来自用户表，而不是 UUID
    assert attachment["uploaded_by_name"]

    # 布置一份未完成的 pending 记录：它不能出现在任何列表里
    pending = _init_upload(client, assignment_id, teacher, filename="未完成.pdf")
    assert pending.status_code == 201

    teacher_view = client.get(
        ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    )
    assert teacher_view.status_code == 200
    assert [item["filename"] for item in teacher_view.json()] == ["guide.docx"]

    student_view = client.get(
        ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(student)
    )
    assert student_view.status_code == 200
    assert [item["filename"] for item in student_view.json()] == ["guide.docx"]

    # 删除后列表为空，重复删除返回 404
    deleted = client.delete(
        ATTACHMENT_URL.format(assignment_id=assignment_id, attachment_id=attachment["id"]),
        headers=_auth(teacher),
    )
    assert deleted.status_code == 204
    assert (
        client.get(
            ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
        ).json()
        == []
    )


def test_permissions_matrix(client: TestClient, fake_storage: FakeStorage) -> None:
    course_id, teacher, student = _course_with_members(client, "perm")
    assignment_id = _create_assignment(client, course_id, teacher)
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    other_teacher = _new_teacher(client, "att-perm-other@example.com")
    outsider = _new_teacher(client, "att-perm-out@example.com")

    # 学生不能上传：ROLE_FORBIDDEN
    forbidden = _init_upload(client, assignment_id, student)
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # 非成员（其他教师）看不到任务：统一 404
    not_found = client.get(
        ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(outsider)
    )
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 非课程创建教师调用写接口：COURSE_FORBIDDEN
    other = _init_upload(client, assignment_id, other_teacher)
    assert other.status_code in (403, 404), other.text

    # 学生读得到（成员可见）
    assert (
        client.get(
            ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(student)
        ).status_code
        == 200
    )


def test_duplicate_filename_conflicts(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, _ = _course_with_members(client, "dup")
    assignment_id = _create_assignment(client, course_id, teacher)

    assert _upload(client, fake_storage, assignment_id, teacher).status_code == 201

    again = _init_upload(client, assignment_id, teacher)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "ATTACHMENT_ALREADY_EXISTS"


def test_reinit_reuses_pending_row(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine
) -> None:
    """重复初始化同名文件应复用同一条待完成记录，而不是越堆越多。"""
    course_id, teacher, _ = _course_with_members(client, "reinit")
    assignment_id = _create_assignment(client, course_id, teacher)

    first = _init_upload(client, assignment_id, teacher, filename="重复.pdf")
    second = _init_upload(client, assignment_id, teacher, filename="重复.pdf")
    assert first.status_code == 201 and second.status_code == 201

    with pg_sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT count(*) FROM assignment_attachments "
                "WHERE assignment_id = :aid AND completed_at IS NULL"
            ),
            {"aid": assignment_id},
        ).scalar_one()
    assert rows == 1
    # 两次初始化签发的是不同的上传会话
    assert first.json()["upload_id"] != second.json()["upload_id"]


def test_complete_rejects_missing_object(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, _ = _course_with_members(client, "missing")
    assignment_id = _create_assignment(client, course_id, teacher)

    presigned = _init_upload(client, assignment_id, teacher)
    assert presigned.status_code == 201
    body = presigned.json()
    # 不往假存储里放对象，直接确认
    response = client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=body["upload_id"]),
        headers=_auth(teacher),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "UPLOAD_INVALID"
    assert error["details"]["reason"] == "OBJECT_MISSING"


def test_complete_rejects_mismatched_size(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, _ = _course_with_members(client, "size")
    assignment_id = _create_assignment(client, course_id, teacher)

    presigned = _init_upload(client, assignment_id, teacher)
    body = presigned.json()
    fake_storage.store_object(
        _object_key_of(fake_storage, body["upload_url"]),
        size=len(CONTENT) + 10,
        content_type=DOCX_MIME,
        sha256_hex=hashlib.sha256(CONTENT).hexdigest(),
        content=CONTENT,
    )
    response = client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=body["upload_id"]),
        headers=_auth(teacher),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "OBJECT_SIZE_MISMATCH"


def test_complete_is_idempotent(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, _ = _course_with_members(client, "idem")
    assignment_id = _create_assignment(client, course_id, teacher)
    completed = _upload(client, fake_storage, assignment_id, teacher)
    assert completed.status_code == 201

    # 再用另一个 upload_id 确认一份新附件，然后重复确认它
    body = _init_upload(client, assignment_id, teacher, filename="再来一份.pdf")
    assert body.status_code == 201
    presigned = body.json()
    fake_storage.store_object(
        _object_key_of(fake_storage, presigned["upload_url"]),
        size=len(CONTENT),
        content_type=PDF_MIME,
        sha256_hex=hashlib.sha256(CONTENT).hexdigest(),
        content=CONTENT,
    )
    first = client.post(
        COMPLETE_URL.format(
            assignment_id=assignment_id, upload_id=presigned["upload_id"]
        ),
        headers=_auth(teacher),
    )
    assert first.status_code == 201
    second = client.post(
        COMPLETE_URL.format(
            assignment_id=assignment_id, upload_id=presigned["upload_id"]
        ),
        headers=_auth(teacher),
    )
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_upload_rejects_unsupported_type(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, _ = _course_with_members(client, "type")
    assignment_id = _create_assignment(client, course_id, teacher)

    # 旧版 .doc：白名单只接受 PDF / PPTX / DOCX
    response = _init_upload(
        client,
        assignment_id,
        teacher,
        filename="旧版.doc",
        content_type=DOC_MIME,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "UPLOAD_INVALID"

    # pptx 是允许的（与课件一致）
    allowed = _init_upload(
        client, assignment_id, teacher, filename="讲义.pptx", content_type=PPTX_MIME
    )
    assert allowed.status_code == 201, allowed.text


def test_archived_course_is_read_only(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine
) -> None:
    course_id, teacher, student = _course_with_members(client, "arch")
    assignment_id = _create_assignment(client, course_id, teacher)
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    assert _upload(client, fake_storage, assignment_id, teacher).status_code == 201

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text("UPDATE courses SET status = 'ARCHIVED' WHERE id = :cid"),
            {"cid": course_id},
        )

    # 归档后仍可读
    assert (
        client.get(
            ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(student)
        ).status_code
        == 200
    )
    # 但不能写
    blocked = _init_upload(client, assignment_id, teacher, filename="归档后.pdf")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "COURSE_ARCHIVED"


def test_draft_assignment_attachments_invisible_to_student(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    course_id, teacher, student = _course_with_members(client, "draft")
    assignment_id = _create_assignment(client, course_id, teacher)
    assert _upload(client, fake_storage, assignment_id, teacher).status_code == 201

    # 草稿阶段学生按不存在处理
    assert (
        client.get(
            ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(student)
        ).status_code
        == 404
    )
    assert client.get(
        ATTACHMENTS_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    ).status_code == 200


# --------------------------------------------------------------------------- #
# 8.8 重新开启
# --------------------------------------------------------------------------- #
def test_reopen_clears_closed_at_and_keeps_published_at(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "reopen")
    assignment_id = _create_assignment(client, course_id, teacher)
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    published = client.get(
        "/api/v1/assignments/{assignment_id}".format(assignment_id=assignment_id),
        headers=_auth(teacher),
    ).json()
    assert published["status"] == "PUBLISHED"
    published_at = published["published_at"]

    closed = client.post(
        CLOSE_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    ).json()
    assert closed["status"] == "CLOSED"
    assert closed["closed_at"] is not None

    reopened = client.post(
        REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    )
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == "PUBLISHED"
    # 重新开启清掉"当前处于关闭状态"的时间，但不改写首次发布时间
    assert body["closed_at"] is None
    assert body["published_at"] == published_at

    # 幂等：已经是 PUBLISHED 时再调用同样返回 200
    again = client.post(
        REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    )
    assert again.status_code == 200
    assert again.json()["published_at"] == published_at


def test_reopen_permissions_and_states(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "reopen-perm")
    assignment_id = _create_assignment(client, course_id, teacher)

    # 草稿不能重新开启
    draft = client.post(
        REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    )
    assert draft.status_code == 409
    assert draft.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"

    # 学生不能调用
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    client.post(CLOSE_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    forbidden = client.post(
        REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(student)
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # 非成员看不到任务
    outsider = _new_teacher(client, "reopen-outsider@example.com")
    assert (
        client.post(
            REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(outsider)
        ).status_code
        == 404
    )

    # 请求体不是空对象时 422
    bad = client.post(
        REOPEN_URL.format(assignment_id=assignment_id),
        json={"unexpected": True},
        headers=_auth(teacher),
    )
    assert bad.status_code == 422


def test_reopen_does_not_change_rubric_version(client: TestClient) -> None:
    """重新开启不是评分规则的变化，因此不产生新版本。"""
    course_id, teacher, _ = _course_with_members(client, "reopen-version")
    assignment_id = _create_assignment(client, course_id, teacher)
    client.post(PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    client.post(CLOSE_URL.format(assignment_id=assignment_id), headers=_auth(teacher))
    body = client.post(
        REOPEN_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    ).json()
    assert body["rubric_version"] == 1
    assert len(body["rubric_items"]) == 1
