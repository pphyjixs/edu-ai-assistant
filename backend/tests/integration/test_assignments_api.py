"""实验任务接口的 PostgreSQL 集成测试（``docs/api-contract.md`` 第 8 节）。

覆盖：创建（含评分版本 1）、列表与草稿可见性、详情、修改（版本策略）、
发布与关闭（幂等与状态机）、归档课程只读、权限矩阵，以及供第 9 节使用的
内部服务（``can_submit`` / 当前评分版本）。

并发与锁序回归见 ``test_assignments_races.py``。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.time import utc_now
from app.modules.assignments import service
from tests.integration.test_materials_api import (
    _auth,
    _create_course,
    _login,
    _register,
)
from tests.integration.test_practice_api import _join

CREATE_URL = "/api/v1/courses/{course_id}/assignments"
DETAIL_URL = "/api/v1/assignments/{assignment_id}"
PUBLISH_URL = "/api/v1/assignments/{assignment_id}/publish"
CLOSE_URL = "/api/v1/assignments/{assignment_id}/close"


@pytest.fixture
def client(db_isolation: None, pg_app) -> Iterator[TestClient]:
    """指向测试库的客户端（实验任务不依赖对象存储与模型）。"""
    with TestClient(pg_app()) as test_client:
        yield test_client


def _payload(**overrides) -> dict:
    payload = {
        "title": "实验一 需求分析",
        "description": "任务说明",
        "total_score": 100,
        "due_at": "2026-09-25T15:59:00Z",
        "allow_late_submission": False,
        "rubric_items": [
            {"title": "需求完整性", "description": "是否完整", "max_score": 40, "order": 1},
            {"title": "建模规范", "description": "是否一致", "max_score": 60, "order": 2},
        ],
    }
    payload.update(overrides)
    return payload


def _create(client: TestClient, course_id: str, teacher: str, **overrides):
    return client.post(
        CREATE_URL.format(course_id=course_id),
        json=_payload(**overrides),
        headers=_auth(teacher),
    )


def _new_teacher(client: TestClient, email: str) -> str:
    """注册并登录一个教师，返回其令牌。"""
    _register(client, email, "TEACHER")
    return _login(client, email)


def _new_student(client: TestClient, email: str) -> str:
    _register(client, email, "STUDENT")
    return _login(client, email)


def _rows(pg_sync_engine, sql: str, **params) -> list[dict]:
    with pg_sync_engine.connect() as connection:
        result = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in result]


# --------------------------------------------------------------------------- #
# 8.2 创建
# --------------------------------------------------------------------------- #
def test_create_assignment_writes_draft_and_version_one(
    client: TestClient, pg_sync_engine
) -> None:
    _register(client, "assign-create@example.com", "TEACHER")
    teacher = _login(client, "assign-create@example.com")
    course_id = _create_course(client, teacher)

    response = _create(client, course_id, teacher)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["rubric_version"] == 1
    assert body["total_score"] == 100.0
    assert body["allow_late_submission"] is False
    assert body["published_at"] is None and body["closed_at"] is None
    assert body["description"] == "任务说明"
    assert [item["order"] for item in body["rubric_items"]] == [1, 2]
    assert [item["max_score"] for item in body["rubric_items"]] == [40.0, 60.0]
    assert all(uuid.UUID(item["id"]) for item in body["rubric_items"])

    versions = _rows(
        pg_sync_engine,
        "SELECT version, total_score FROM assignment_rubric_versions"
        " WHERE assignment_id = CAST(:id AS uuid)",
        id=body["id"],
    )
    assert [(row["version"], float(row["total_score"])) for row in versions] == [(1, 100.0)]
    items = _rows(
        pg_sync_engine,
        "SELECT title, max_score, \"order\" FROM assignment_rubric_items"
        " WHERE rubric_version_id IN (SELECT id FROM assignment_rubric_versions"
        " WHERE assignment_id = CAST(:id AS uuid)) ORDER BY \"order\"",
        id=body["id"],
    )
    assert [(row["title"], float(row["max_score"])) for row in items] == [
        ("需求完整性", 40.0),
        ("建模规范", 60.0),
    ]
    # 当前评分版本指针已回填
    assert _rows(
        pg_sync_engine,
        "SELECT current_rubric_version_id IS NOT NULL AS filled FROM assignments"
        " WHERE id = CAST(:id AS uuid)",
        id=body["id"],
    )[0]["filled"] is True


def test_create_rejects_rubric_mismatch_without_side_effects(
    client: TestClient, pg_sync_engine
) -> None:
    _register(client, "assign-mismatch@example.com", "TEACHER")
    teacher = _login(client, "assign-mismatch@example.com")
    course_id = _create_course(client, teacher)

    response = _create(
        client,
        course_id,
        teacher,
        rubric_items=[
            {"title": "A", "max_score": 40, "order": 1},
            {"title": "B", "max_score": 50, "order": 2},
        ],
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RUBRIC_SCORE_MISMATCH"
    assert _rows(pg_sync_engine, "SELECT id FROM assignments") == []
    assert _rows(pg_sync_engine, "SELECT id FROM assignment_rubric_versions") == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"unknown_field": 1},
        {"description": None},
        {"total_score": True},
        {"total_score": "100"},
        {"allow_late_submission": 1},
        {"due_at": "2026-09-25T15:59:00"},
        {"rubric_items": []},
        {
            "rubric_items": [
                {"title": "A", "max_score": 100, "order": 2},
            ]
        },
    ],
)
def test_create_rejects_invalid_payload(
    client: TestClient, pg_sync_engine, overrides: dict
) -> None:
    _register(client, "assign-invalid@example.com", "TEACHER")
    teacher = _login(client, "assign-invalid@example.com")
    course_id = _create_course(client, teacher)

    response = _create(client, course_id, teacher, **overrides)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"VALIDATION_ERROR", "RUBRIC_SCORE_MISMATCH"}
    assert _rows(pg_sync_engine, "SELECT id FROM assignments") == []


def test_create_permissions(client: TestClient, pg_sync_engine) -> None:
    _register(client, "assign-perm@example.com", "TEACHER")
    teacher = _login(client, "assign-perm@example.com")
    course_id = _create_course(client, teacher)

    # 匿名 401
    assert client.post(CREATE_URL.format(course_id=course_id), json=_payload()).status_code == 401

    # 非成员教师 → 404（不暴露课程是否存在）
    outsider = _new_teacher(client, "assign-outsider@example.com")
    outsider_response = _create(client, course_id, outsider)
    assert outsider_response.status_code == 404, outsider_response.text
    assert outsider_response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 学生（课程成员）→ 403 ROLE_FORBIDDEN
    student = _new_student(client, "assign-student@example.com")
    _join(client, course_id, teacher, student)
    student_response = _create(client, course_id, student)
    assert student_response.status_code == 403, student_response.text
    assert student_response.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # 课程内的其他教师（非创建者）：加入接口仅对学生开放，
    # 这里直接构造成员记录来覆盖 403 COURSE_FORBIDDEN 分支
    colleague = _new_teacher(client, "assign-colleague@example.com")
    colleague_id = client.get(
        "/api/v1/users/me", headers=_auth(colleague)
    ).json()["id"]
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO course_members (id, course_id, user_id, course_role, joined_at)"
                " VALUES (CAST(:id AS uuid), CAST(:course_id AS uuid),"
                " CAST(:user_id AS uuid), 'TEACHER'::course_role, now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "course_id": course_id,
                "user_id": colleague_id,
            },
        )
    colleague_response = _create(client, course_id, colleague)
    assert colleague_response.status_code == 403, colleague_response.text
    assert colleague_response.json()["error"]["code"] == "COURSE_FORBIDDEN"


# --------------------------------------------------------------------------- #
# 8.3 列表 / 8.4 详情
# --------------------------------------------------------------------------- #
def _course_with_members(
    client: TestClient, suffix: str
) -> tuple[str, str, str]:
    """建课程并加入一名学生，返回 ``(课程 ID, 教师令牌, 学生令牌)``。"""
    teacher = _new_teacher(client, f"assign-{suffix}-t@example.com")
    course_id = _create_course(client, teacher)
    student = _new_student(client, f"assign-{suffix}-s@example.com")
    _join(client, course_id, teacher, student)
    return course_id, teacher, student


def test_list_shows_drafts_to_teacher_only(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "list")
    first = _create(client, course_id, teacher).json()
    _create(client, course_id, teacher, title="第二份任务")

    teacher_view = client.get(
        CREATE_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert teacher_view.status_code == 200
    body = teacher_view.json()
    assert body["total"] == 2
    assert body["page"] == 1 and body["page_size"] == 20
    # 列表使用摘要 Schema：不含说明与评分项
    assert set(body["items"][0]) == {
        "id",
        "course_id",
        "title",
        "total_score",
        "due_at",
        "allow_late_submission",
        "status",
        "rubric_version",
        "published_at",
        "closed_at",
        "created_at",
        "updated_at",
    }
    assert body["items"][0]["rubric_version"] == 1
    assert body["items"][0]["total_score"] == 100.0

    # 学生看不到草稿
    student_view = client.get(
        CREATE_URL.format(course_id=course_id), headers=_auth(student)
    )
    assert student_view.status_code == 200
    assert student_view.json()["items"] == []
    assert student_view.json()["total"] == 0

    # 发布后学生可见
    assert client.post(
        PUBLISH_URL.format(assignment_id=first["id"]), headers=_auth(teacher)
    ).status_code == 200
    after_publish = client.get(
        CREATE_URL.format(course_id=course_id), headers=_auth(student)
    ).json()
    assert [item["id"] for item in after_publish["items"]] == [first["id"]]


def test_list_order_and_pagination(client: TestClient, pg_sync_engine) -> None:
    course_id, teacher, _student = _course_with_members(client, "page")
    for index in range(3):
        assert _create(client, course_id, teacher, title=f"任务 {index}").status_code == 201

    rows = _rows(
        pg_sync_engine,
        "SELECT id::text AS id FROM assignments WHERE course_id = CAST(:id AS uuid)"
        " ORDER BY created_at DESC, id DESC",
        id=course_id,
    )
    expected = [row["id"] for row in rows]

    first_page = client.get(
        CREATE_URL.format(course_id=course_id),
        params={"page": 1, "page_size": 2},
        headers=_auth(teacher),
    ).json()
    second_page = client.get(
        CREATE_URL.format(course_id=course_id),
        params={"page": 2, "page_size": 2},
        headers=_auth(teacher),
    ).json()

    assert [item["id"] for item in first_page["items"]] == expected[:2]
    assert [item["id"] for item in second_page["items"]] == expected[2:]
    assert first_page["total"] == 3 and second_page["total"] == 3
    # 分页越界 422
    assert client.get(
        CREATE_URL.format(course_id=course_id),
        params={"page_size": 101},
        headers=_auth(teacher),
    ).status_code == 422


def test_list_visibility_boundaries(client: TestClient) -> None:
    course_id, teacher, _student = _course_with_members(client, "listperm")
    outsider = _new_teacher(client, "assign-list-out@example.com")

    assert client.get(CREATE_URL.format(course_id=course_id)).status_code == 401
    assert (
        client.get(
            CREATE_URL.format(course_id=course_id), headers=_auth(outsider)
        ).status_code
        == 404
    )
    assert (
        client.get(
            CREATE_URL.format(course_id=str(uuid.uuid4())), headers=_auth(teacher)
        ).status_code
        == 404
    )


def test_detail_visibility_and_current_rubric(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "detail")
    created = _create(client, course_id, teacher).json()
    detail_url = DETAIL_URL.format(assignment_id=created["id"])

    teacher_view = client.get(detail_url, headers=_auth(teacher))
    assert teacher_view.status_code == 200
    assert teacher_view.json()["status"] == "DRAFT"
    assert teacher_view.json()["description"] == "任务说明"
    assert len(teacher_view.json()["rubric_items"]) == 2

    # 学生看不到草稿；不存在与非成员统一 404；匿名 401
    student_view = client.get(detail_url, headers=_auth(student))
    assert student_view.status_code == 404
    assert student_view.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert client.get(detail_url).status_code == 401
    assert (
        client.get(
            DETAIL_URL.format(assignment_id=str(uuid.uuid4())), headers=_auth(teacher)
        ).status_code
        == 404
    )

    # 发布后学生可见
    client.post(PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher))
    published = client.get(detail_url, headers=_auth(student))
    assert published.status_code == 200
    assert published.json()["rubric_version"] == 1
    assert published.json()["published_at"] is not None


# --------------------------------------------------------------------------- #
# 8.5 修改（含版本策略）
# --------------------------------------------------------------------------- #
def test_update_non_rubric_fields_keeps_version(client: TestClient, pg_sync_engine) -> None:
    course_id, teacher, _student = _course_with_members(client, "upd-plain")
    created = _create(client, course_id, teacher).json()

    response = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={
            "title": "  实验一（改）  ",
            "description": "新说明",
            "due_at": "2026-10-01T00:00:00Z",
            "allow_late_submission": True,
        },
        headers=_auth(teacher),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "实验一（改）"
    assert body["description"] == "新说明"
    assert body["allow_late_submission"] is True
    assert body["rubric_version"] == 1
    assert body["total_score"] == 100.0
    assert len(
        _rows(
            pg_sync_engine,
            "SELECT version FROM assignment_rubric_versions"
            " WHERE assignment_id = CAST(:id AS uuid)",
            id=created["id"],
        )
    ) == 1


def test_update_due_at_null_clears_deadline(client: TestClient) -> None:
    course_id, teacher, _student = _course_with_members(client, "upd-due")
    created = _create(client, course_id, teacher).json()
    assert created["due_at"] is not None

    response = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={"due_at": None},
        headers=_auth(teacher),
    )

    assert response.status_code == 200, response.text
    assert response.json()["due_at"] is None


def test_update_rubric_appends_version_and_keeps_history(
    client: TestClient, pg_sync_engine
) -> None:
    course_id, teacher, _student = _course_with_members(client, "upd-rubric")
    created = _create(client, course_id, teacher).json()
    original_ids = sorted(item["id"] for item in created["rubric_items"])

    response = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={
            "rubric_items": [
                {"title": "需求", "description": "新", "max_score": 70, "order": 1},
                {"title": "建模", "description": "", "max_score": 30, "order": 2},
            ]
        },
        headers=_auth(teacher),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rubric_version"] == 2
    assert [item["max_score"] for item in body["rubric_items"]] == [70.0, 30.0]
    assert body["total_score"] == 100.0
    # 新版本生成新的评分项 ID
    assert not (set(original_ids) & {item["id"] for item in body["rubric_items"]})

    # 版本 1 的评分项保持原样
    version_one = _rows(
        pg_sync_engine,
        "SELECT i.title, i.max_score FROM assignment_rubric_items i"
        " JOIN assignment_rubric_versions v ON v.id = i.rubric_version_id"
        " WHERE v.assignment_id = CAST(:id AS uuid) AND v.version = 1"
        " ORDER BY i.\"order\"",
        id=created["id"],
    )
    assert [(row["title"], float(row["max_score"])) for row in version_one] == [
        ("需求完整性", 40.0),
        ("建模规范", 60.0),
    ]
    assert sorted(item["id"] for item in created["rubric_items"]) == original_ids


def test_update_with_identical_rubric_or_total_does_not_add_version(
    client: TestClient, pg_sync_engine
) -> None:
    course_id, teacher, _student = _course_with_members(client, "upd-same")
    created = _create(client, course_id, teacher).json()
    same_items = [
        {
            "title": item["title"],
            "description": item["description"],
            "max_score": item["max_score"],
            "order": item["order"],
        }
        for item in created["rubric_items"]
    ]

    first = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={"rubric_items": same_items},
        headers=_auth(teacher),
    )
    second = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={"total_score": 100},
        headers=_auth(teacher),
    )

    assert first.status_code == 200 and first.json()["rubric_version"] == 1
    assert second.status_code == 200 and second.json()["rubric_version"] == 1
    assert len(
        _rows(
            pg_sync_engine,
            "SELECT version FROM assignment_rubric_versions"
            " WHERE assignment_id = CAST(:id AS uuid)",
            id=created["id"],
        )
    ) == 1


def test_update_total_only_uses_current_rubric_as_candidate(client: TestClient) -> None:
    course_id, teacher, _student = _course_with_members(client, "upd-total")
    created = _create(client, course_id, teacher).json()
    url = DETAIL_URL.format(assignment_id=created["id"])

    mismatch = client.patch(url, json={"total_score": 50}, headers=_auth(teacher))
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["error"]["code"] == "RUBRIC_SCORE_MISMATCH"

    # 总分与"当前评分项之和"一致时允许（此例为 100）
    ok = client.patch(url, json={"total_score": 100}, headers=_auth(teacher))
    assert ok.status_code == 200
    assert ok.json()["total_score"] == 100.0


def test_update_body_and_state_errors(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "upd-err")
    created = _create(client, course_id, teacher).json()
    url = DETAIL_URL.format(assignment_id=created["id"])

    for payload in ({}, {"unknown": 1}, {"description": None}, {"total_score": "100"}):
        response = client.patch(url, json=payload, headers=_auth(teacher))
        assert response.status_code == 422, payload

    # 权限
    assert client.patch(url, json={"title": "x"}, headers=_auth(student)).status_code == 403
    outsider = _new_teacher(client, "assign-upd-out@example.com")
    assert (
        client.patch(url, json={"title": "x"}, headers=_auth(outsider)).status_code == 404
    )
    assert client.patch(url, json={"title": "x"}).status_code == 401

    # 关闭后不可修改
    client.post(PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher))
    client.post(CLOSE_URL.format(assignment_id=created["id"]), headers=_auth(teacher))
    closed = client.patch(url, json={"title": "x"}, headers=_auth(teacher))
    assert closed.status_code == 409, closed.text
    assert closed.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"


# --------------------------------------------------------------------------- #
# 8.6 发布 / 8.7 关闭
# --------------------------------------------------------------------------- #
def test_publish_is_idempotent_and_sets_published_at(client: TestClient) -> None:
    course_id, teacher, _student = _course_with_members(client, "publish")
    created = _create(client, course_id, teacher).json()
    url = PUBLISH_URL.format(assignment_id=created["id"])

    first = client.post(url, headers=_auth(teacher))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "PUBLISHED"
    published_at = first.json()["published_at"]
    assert published_at is not None

    second = client.post(url, json={}, headers=_auth(teacher))
    assert second.status_code == 200
    assert second.json()["status"] == "PUBLISHED"
    assert second.json()["published_at"] == published_at


def test_close_requires_published_and_is_idempotent(client: TestClient) -> None:
    course_id, teacher, _student = _course_with_members(client, "close")
    created = _create(client, course_id, teacher).json()
    close_url = CLOSE_URL.format(assignment_id=created["id"])

    # 草稿不能关闭
    draft = client.post(close_url, headers=_auth(teacher))
    assert draft.status_code == 409, draft.text
    assert draft.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"

    client.post(PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher))
    first = client.post(close_url, headers=_auth(teacher))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "CLOSED"
    closed_at = first.json()["closed_at"]
    assert closed_at is not None

    second = client.post(close_url, json={}, headers=_auth(teacher))
    assert second.status_code == 200
    assert second.json()["closed_at"] == closed_at

    # 已关闭不能重新发布
    republish = client.post(
        PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
    )
    assert republish.status_code == 409
    assert republish.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"


@pytest.mark.parametrize("body", [b"null", b"[]", b"1", b"not-json", b'{"extra":1}', b"\xff"])
def test_publish_and_close_reject_non_empty_object(client: TestClient, body: bytes) -> None:
    """请求体错误在状态检查之后返回：DRAFT 校验发布、PUBLISHED 校验关闭。"""
    course_id, teacher, _student = _course_with_members(client, "body")
    created = _create(client, course_id, teacher).json()
    publish_url = PUBLISH_URL.format(assignment_id=created["id"])
    close_url = CLOSE_URL.format(assignment_id=created["id"])
    headers = {**_auth(teacher), "Content-Type": "application/json"}

    # DRAFT：发布允许（状态通过），因此暴露请求体错误
    rejected = client.post(publish_url, content=body, headers=headers)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"

    # DRAFT 关闭属于状态错误（409 先于 422）
    blocked = client.post(close_url, content=body, headers=headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"

    detail = client.get(
        DETAIL_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
    ).json()
    assert detail["status"] == "DRAFT"
    assert detail["published_at"] is None

    # 发布后再校验关闭的请求体
    assert client.post(publish_url, headers=_auth(teacher)).status_code == 200
    published_at = client.get(
        DETAIL_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
    ).json()["published_at"]

    rejected_close = client.post(close_url, content=body, headers=headers)
    assert rejected_close.status_code == 422, rejected_close.text
    assert rejected_close.json()["error"]["code"] == "VALIDATION_ERROR"

    after = client.get(
        DETAIL_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
    ).json()
    assert after["status"] == "PUBLISHED"
    assert after["closed_at"] is None
    assert after["published_at"] == published_at


def test_publish_close_permissions(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "pubperm")
    created = _create(client, course_id, teacher).json()
    outsider = _new_teacher(client, "assign-pub-out@example.com")
    publish = PUBLISH_URL.format(assignment_id=created["id"])
    close = CLOSE_URL.format(assignment_id=created["id"])

    for url in (publish, close):
        assert client.post(url, headers=_auth(student)).status_code == 403
        assert client.post(url, headers=_auth(outsider)).status_code == 404
        assert client.post(url).status_code == 401


# --------------------------------------------------------------------------- #
# 归档课程：可读历史、禁止写入
# --------------------------------------------------------------------------- #
def test_archived_course_is_read_only(client: TestClient) -> None:
    course_id, teacher, student = _course_with_members(client, "archived")
    created = _create(client, course_id, teacher).json()
    detail_url = DETAIL_URL.format(assignment_id=created["id"])
    client.post(PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher))

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200

    # 读历史仍 200
    assert client.get(
        CREATE_URL.format(course_id=course_id), headers=_auth(teacher)
    ).status_code == 200
    assert client.get(detail_url, headers=_auth(student)).status_code == 200

    # 写操作 409 COURSE_ARCHIVED
    assert _create(client, course_id, teacher).status_code == 409
    assert (
        client.patch(detail_url, json={"title": "x"}, headers=_auth(teacher)).status_code
        == 409
    )
    assert (
        client.post(
            PUBLISH_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
        ).status_code
        == 409
    )
    assert (
        client.post(
            CLOSE_URL.format(assignment_id=created["id"]), headers=_auth(teacher)
        ).status_code
        == 409
    )


def test_archived_error_precedes_body_validation(client: TestClient) -> None:
    """错误优先级：归档（409）先于请求体结构与字段（422）。"""
    course_id, teacher, _student = _course_with_members(client, "priority")
    created = _create(client, course_id, teacher).json()
    client.post(f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher))

    malformed = client.post(
        CREATE_URL.format(course_id=course_id),
        content=b"not-json",
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert malformed.status_code == 409, malformed.text
    assert malformed.json()["error"]["code"] == "COURSE_ARCHIVED"

    patch_malformed = client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        content=b'{"unknown":1}',
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert patch_malformed.status_code == 409
    assert patch_malformed.json()["error"]["code"] == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 内部服务（供第 9 节 Submission 使用）
# --------------------------------------------------------------------------- #
def test_internal_submission_service_contract(client: TestClient, pg_session_factory) -> None:
    """``can_submit`` 与"当前评分规则版本"可供批改/提交模块直接复用。"""
    import asyncio

    from app.modules.assignments import repository as repo

    course_id, teacher, _student = _course_with_members(client, "internal")
    created = _create(client, course_id, teacher).json()
    assignment_id = uuid.UUID(created["id"])
    due_at = datetime.fromisoformat(created["due_at"].replace("Z", "+00:00"))

    async def load_and_check(publish: bool) -> tuple[bool, bool, uuid.UUID | None]:
        async with pg_session_factory() as session:
            assignment = await repo.get_assignment_by_id(session, assignment_id)
            assert assignment is not None
            if publish:
                await service.publish_assignment(session, assignment=assignment)
            loaded = await repo.get_assignment_by_id(session, assignment_id)
            assert loaded is not None
            return (
                service.can_submit(loaded, now=utc_now()),
                # now == due_at 视为已经截止
                service.can_submit(loaded, now=due_at),
                service.current_rubric_version_id(loaded),
            )

    # DRAFT：任何时刻都不可提交
    draft_submit, _, _ = asyncio.run(load_and_check(publish=False))
    assert draft_submit is False

    # 发布后：截止前可提交，now == due_at 时不可提交
    can_now, can_at_due, version_id = asyncio.run(load_and_check(publish=True))
    assert can_now is True
    assert can_at_due is False
    assert version_id is not None

    # 允许补交后，超过截止时间仍可提交
    assert client.patch(
        DETAIL_URL.format(assignment_id=created["id"]),
        json={"allow_late_submission": True},
        headers=_auth(teacher),
    ).status_code == 200

    async def late_allowed() -> bool:
        async with pg_session_factory() as session:
            loaded = await repo.get_assignment_by_id(session, assignment_id)
            assert loaded is not None
            return service.can_submit(loaded, now=due_at)

    assert asyncio.run(late_allowed()) is True

    # 关闭后不可提交（手工关闭优先于 allow_late_submission）
    client.post(CLOSE_URL.format(assignment_id=created["id"]), headers=_auth(teacher))

    async def after_close() -> bool:
        async with pg_session_factory() as session:
            loaded = await repo.get_assignment_by_id(session, assignment_id)
            assert loaded is not None
            return service.can_submit(loaded, now=utc_now())

    assert asyncio.run(after_close()) is False
