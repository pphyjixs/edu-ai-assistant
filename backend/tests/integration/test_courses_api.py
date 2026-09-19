"""课程接口集成测试（真实 PostgreSQL）。

覆盖 ``docs/api-contract.md`` 第 3 节的全部 8 个接口，以及分页、
邀请码轮换、归档后读写、首次/重复加入与并发成员唯一性。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import text

from app.modules.courses import repository as course_repo
from app.modules.courses import service as course_service

PASSWORD = "Demo password 2026!"
COURSES_URL = "/api/v1/courses"


def _register(client, email: str, role: str = "STUDENT", *, name: str = "成员"):
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
    return response.json()


def _login(client, email: str) -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _teacher(client, email: str = "teacher@example.com") -> dict:
    _register(client, email, "TEACHER", name="张老师")
    return _login(client, email)


def _student(client, email: str, name: str = "学生") -> dict:
    _register(client, email, "STUDENT", name=name)
    return _login(client, email)


def _create_course(client, token: str, name: str = "软件工程实验", **extra) -> dict:
    response = client.post(
        COURSES_URL,
        json={"name": name, "description": "课程说明", **extra},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------- 异步版本（并发用例用） ---------------------- #
async def _a_register(client, email: str, role: str, *, name: str) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": name,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text


async def _a_login(client, email: str) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _a_teacher(client, email: str = "teacher@example.com") -> dict:
    await _a_register(client, email, "TEACHER", name="张老师")
    return await _a_login(client, email)


async def _a_student(client, email: str, name: str = "学生") -> dict:
    await _a_register(client, email, "STUDENT", name=name)
    return await _a_login(client, email)


async def _a_create_course(client, token: str, name: str = "软件工程实验") -> dict:
    response = await client.post(
        COURSES_URL,
        json={"name": name, "description": "课程说明"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 创建课程
# --------------------------------------------------------------------------- #
def test_teacher_creates_course_and_gets_invite_code(api_client) -> None:
    teacher = _teacher(api_client)

    body = _create_course(api_client, teacher["access_token"])

    assert set(body) == {
        "id",
        "name",
        "description",
        "teacher_id",
        "status",
        "created_at",
        "updated_at",
        "invite_code",
    }
    assert body["name"] == "软件工程实验"
    assert body["status"] == "ACTIVE"
    assert body["teacher_id"] == teacher["user"]["id"]
    # 12 位大写字母与数字
    assert len(body["invite_code"]) == 12
    assert body["invite_code"].isalnum() and body["invite_code"].isupper()
    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")


def test_creating_course_registers_creator_as_teacher_member(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])

    members = api_client.get(
        f"{COURSES_URL}/{course['id']}/members", headers=_auth(teacher["access_token"])
    ).json()

    assert members["total"] == 1
    assert members["items"][0]["course_role"] == "TEACHER"
    assert members["items"][0]["user_id"] == teacher["user"]["id"]


def test_student_cannot_create_course(api_client) -> None:
    student = _student(api_client, "student@example.com")

    response = api_client.post(
        COURSES_URL, json={"name": "课程"}, headers=_auth(student["access_token"])
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_create_course_rejects_unknown_field_and_null(api_client) -> None:
    teacher = _teacher(api_client)
    headers = _auth(teacher["access_token"])

    unknown = api_client.post(
        COURSES_URL, json={"name": "课程", "status": "ARCHIVED"}, headers=headers
    )
    explicit_null = api_client.post(
        COURSES_URL, json={"name": "课程", "description": None}, headers=headers
    )

    assert unknown.status_code == 422
    assert explicit_null.status_code == 422


def test_create_course_requires_authentication(api_client) -> None:
    response = api_client.post(COURSES_URL, json={"name": "课程"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


# --------------------------------------------------------------------------- #
# 课程列表
# --------------------------------------------------------------------------- #
def test_list_returns_only_courses_i_joined(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    outsider = _student(api_client, "outsider@example.com")

    first = _create_course(api_client, teacher["access_token"], "课程一")
    second = _create_course(api_client, teacher["access_token"], "课程二")
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": first["invite_code"]},
        headers=_auth(student["access_token"]),
    )

    teacher_page = api_client.get(COURSES_URL, headers=_auth(teacher["access_token"])).json()
    student_page = api_client.get(COURSES_URL, headers=_auth(student["access_token"])).json()
    outsider_page = api_client.get(COURSES_URL, headers=_auth(outsider["access_token"])).json()

    assert teacher_page["total"] == 2
    assert student_page["total"] == 1
    assert student_page["items"][0]["id"] == first["id"]
    assert outsider_page["total"] == 0
    assert second["id"] != first["id"]


def test_list_is_paginated_and_newest_first(api_client) -> None:
    teacher = _teacher(api_client)
    names = ["课程一", "课程二", "课程三"]
    for name in names:
        _create_course(api_client, teacher["access_token"], name)
    headers = _auth(teacher["access_token"])

    first_page = api_client.get(COURSES_URL, params={"page": 1, "page_size": 2}, headers=headers).json()
    second_page = api_client.get(COURSES_URL, params={"page": 2, "page_size": 2}, headers=headers).json()

    assert first_page["page"] == 1 and first_page["page_size"] == 2
    assert first_page["total"] == 3
    assert [item["name"] for item in first_page["items"]] == ["课程三", "课程二"]
    assert [item["name"] for item in second_page["items"]] == ["课程一"]


def test_list_rejects_invalid_pagination(api_client) -> None:
    teacher = _teacher(api_client)
    headers = _auth(teacher["access_token"])

    assert api_client.get(COURSES_URL, params={"page": 0}, headers=headers).status_code == 422
    assert api_client.get(COURSES_URL, params={"page_size": 101}, headers=headers).status_code == 422


def test_list_response_never_contains_invite_code(api_client) -> None:
    teacher = _teacher(api_client)
    _create_course(api_client, teacher["access_token"])

    page = api_client.get(COURSES_URL, headers=_auth(teacher["access_token"])).json()

    assert "invite_code" not in page["items"][0]
    assert "invite_code" not in str(page)


# --------------------------------------------------------------------------- #
# 课程详情
# --------------------------------------------------------------------------- #
def test_detail_invite_code_visibility(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    course = _create_course(api_client, teacher["access_token"])
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student["access_token"]),
    )

    teacher_view = api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(teacher["access_token"])
    ).json()
    student_view = api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(student["access_token"])
    ).json()

    assert teacher_view["invite_code"] == course["invite_code"]
    assert "invite_code" not in student_view, "学生详情不得出现邀请码"


def test_detail_forbidden_for_non_member(api_client) -> None:
    teacher = _teacher(api_client)
    outsider = _student(api_client, "outsider@example.com")
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(outsider["access_token"])
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "COURSE_FORBIDDEN"


def test_detail_not_found_for_unknown_course(api_client) -> None:
    teacher = _teacher(api_client)

    response = api_client.get(
        f"{COURSES_URL}/{uuid.uuid4()}", headers=_auth(teacher["access_token"])
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_detail_rejects_non_uuid(api_client) -> None:
    teacher = _teacher(api_client)

    response = api_client.get(f"{COURSES_URL}/not-a-uuid", headers=_auth(teacher["access_token"]))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# 修改课程
# --------------------------------------------------------------------------- #
def test_creator_updates_course(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.patch(
        f"{COURSES_URL}/{course['id']}",
        json={"name": "  新名称  ", "description": "新说明"},
        headers=_auth(teacher["access_token"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "新名称", "名称去除首尾空白后保存"
    assert body["description"] == "新说明"
    assert body["updated_at"] >= course["updated_at"]
    assert body["invite_code"] == course["invite_code"]


def test_update_can_clear_description(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])

    body = api_client.patch(
        f"{COURSES_URL}/{course['id']}",
        json={"description": ""},
        headers=_auth(teacher["access_token"]),
    ).json()

    assert body["description"] == ""
    assert body["name"] == course["name"], "省略的字段保持原值"


def test_update_rejects_empty_body(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.patch(
        f"{COURSES_URL}/{course['id']}", json={}, headers=_auth(teacher["access_token"])
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_non_creator_cannot_update_course(api_client) -> None:
    teacher = _teacher(api_client)
    other_teacher = _teacher(api_client, "other.teacher@example.com")
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.patch(
        f"{COURSES_URL}/{course['id']}",
        json={"name": "改名"},
        headers=_auth(other_teacher["access_token"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "COURSE_FORBIDDEN"


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #
def test_creator_archives_course(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.post(
        f"{COURSES_URL}/{course['id']}/archive", headers=_auth(teacher["access_token"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ARCHIVED"
    assert "invite_code" not in body, "归档响应不含邀请码"


def test_archive_is_idempotent(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])
    headers = _auth(teacher["access_token"])

    first = api_client.post(f"{COURSES_URL}/{course['id']}/archive", headers=headers).json()
    second = api_client.post(f"{COURSES_URL}/{course['id']}/archive", headers=headers)

    assert second.status_code == 200
    assert second.json() == first


def test_archived_course_still_readable_without_invite_code(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    course = _create_course(api_client, teacher["access_token"])
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student["access_token"]),
    )
    api_client.post(f"{COURSES_URL}/{course['id']}/archive", headers=_auth(teacher["access_token"]))

    detail = api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(teacher["access_token"])
    ).json()
    member_detail = api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(student["access_token"])
    )
    listed = api_client.get(COURSES_URL, headers=_auth(teacher["access_token"])).json()

    assert detail["status"] == "ARCHIVED"
    assert "invite_code" not in detail, "已归档课程对创建教师也省略邀请码"
    assert member_detail.status_code == 200, "归档课程成员仍可读取"
    assert listed["total"] == 1, "归档课程仍出现在列表中"


def test_archived_course_rejects_writes(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    course = _create_course(api_client, teacher["access_token"])
    invite_code = course["invite_code"]
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": invite_code},
        headers=_auth(student["access_token"]),
    )
    api_client.post(f"{COURSES_URL}/{course['id']}/archive", headers=_auth(teacher["access_token"]))

    update = api_client.patch(
        f"{COURSES_URL}/{course['id']}",
        json={"name": "改名"},
        headers=_auth(teacher["access_token"]),
    )
    reset = api_client.post(
        f"{COURSES_URL}/{course['id']}/invite-code",
        headers=_auth(teacher["access_token"]),
    )
    # 已加入的学生再次加入归档课程，同样返回 409
    rejoin = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": invite_code},
        headers=_auth(student["access_token"]),
    )

    for response in (update, reset, rejoin):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 邀请码
# --------------------------------------------------------------------------- #
def test_reset_invite_code_invalidates_old_code(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    course = _create_course(api_client, teacher["access_token"])
    old_code = course["invite_code"]

    response = api_client.post(
        f"{COURSES_URL}/{course['id']}/invite-code",
        headers=_auth(teacher["access_token"]),
    )

    assert response.status_code == 200
    new_code = response.json()["invite_code"]
    assert new_code != old_code

    stale = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": old_code},
        headers=_auth(student["access_token"]),
    )
    fresh = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": new_code},
        headers=_auth(student["access_token"]),
    )

    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "INVITE_CODE_INVALID"
    assert fresh.status_code == 201


def test_reset_does_not_reissue_the_current_code(api_client, monkeypatch) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])
    old_code = course["invite_code"]
    replacement = "ZZ99YY88XX77"
    assert replacement != old_code
    candidates = iter((old_code, replacement))
    monkeypatch.setattr(course_service, "generate_invite_code", lambda: next(candidates))

    response = api_client.post(
        f"{COURSES_URL}/{course['id']}/invite-code",
        headers=_auth(teacher["access_token"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["invite_code"] == replacement


def test_non_creator_cannot_reset_invite_code(api_client) -> None:
    teacher = _teacher(api_client)
    other = _teacher(api_client, "other.teacher@example.com")
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.post(
        f"{COURSES_URL}/{course['id']}/invite-code",
        headers=_auth(other["access_token"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "COURSE_FORBIDDEN"


# --------------------------------------------------------------------------- #
# 加入课程
# --------------------------------------------------------------------------- #
def test_first_join_returns_201_and_repeat_returns_200(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com")
    course = _create_course(api_client, teacher["access_token"])

    first = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student["access_token"]),
    )
    second = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student["access_token"]),
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert "invite_code" not in first.json()

    members = api_client.get(
        f"{COURSES_URL}/{course['id']}/members", headers=_auth(teacher["access_token"])
    ).json()
    assert members["total"] == 2, "重复加入不得新增成员记录"


def test_join_with_unknown_code_returns_422(api_client) -> None:
    student = _student(api_client, "student@example.com")

    response = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": "NOPE12345678"},
        headers=_auth(student["access_token"]),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVITE_CODE_INVALID"


def test_teacher_cannot_join_course(api_client) -> None:
    teacher = _teacher(api_client)

    response = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": "AB12CD34EF56"},
        headers=_auth(teacher["access_token"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


# --------------------------------------------------------------------------- #
# 成员列表
# --------------------------------------------------------------------------- #
def test_members_list_requires_creator(api_client) -> None:
    teacher = _teacher(api_client)
    student = _student(api_client, "student@example.com", name="李同学")
    course = _create_course(api_client, teacher["access_token"])
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student["access_token"]),
    )

    forbidden = api_client.get(
        f"{COURSES_URL}/{course['id']}/members", headers=_auth(student["access_token"])
    )
    allowed = api_client.get(
        f"{COURSES_URL}/{course['id']}/members", headers=_auth(teacher["access_token"])
    )

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "COURSE_FORBIDDEN"
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["total"] == 2
    # 创建教师先加入，按加入时间正序排在前面
    assert [item["course_role"] for item in body["items"]] == ["TEACHER", "STUDENT"]
    assert body["items"][1]["display_name"] == "李同学"
    assert "email" not in str(body), "成员列表不得返回邮箱"


def test_members_list_is_paginated(api_client) -> None:
    teacher = _teacher(api_client)
    course = _create_course(api_client, teacher["access_token"])
    for index in range(3):
        student = _student(api_client, f"student{index}@example.com", name=f"学生{index}")
        api_client.post(
            f"{COURSES_URL}/join",
            json={"invite_code": course["invite_code"]},
            headers=_auth(student["access_token"]),
        )
    headers = _auth(teacher["access_token"])

    first_page = api_client.get(
        f"{COURSES_URL}/{course['id']}/members",
        params={"page": 1, "page_size": 2},
        headers=headers,
    ).json()
    second_page = api_client.get(
        f"{COURSES_URL}/{course['id']}/members",
        params={"page": 2, "page_size": 2},
        headers=headers,
    ).json()

    assert first_page["total"] == 4
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 2
    assert first_page["items"][0]["course_role"] == "TEACHER"


# --------------------------------------------------------------------------- #
# 权限与错误边界
# --------------------------------------------------------------------------- #
def test_default_page_size_is_twenty(api_client) -> None:
    teacher = _teacher(api_client)
    _create_course(api_client, teacher["access_token"])

    page = api_client.get(COURSES_URL, headers=_auth(teacher["access_token"])).json()

    assert page["page"] == 1
    assert page["page_size"] == 20


def test_other_teacher_cannot_read_members(api_client) -> None:
    """平台角色是教师，也不代表能看别人的成员列表。"""
    teacher = _teacher(api_client)
    other_teacher = _teacher(api_client, "other.teacher@example.com")
    course = _create_course(api_client, teacher["access_token"])

    response = api_client.get(
        f"{COURSES_URL}/{course['id']}/members",
        headers=_auth(other_teacher["access_token"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "COURSE_FORBIDDEN"


def test_archived_status_is_not_leaked_to_outiders(api_client) -> None:
    """无权限者先被权限检查拦下（403），不会拿到 409 而得知课程已归档。"""
    teacher = _teacher(api_client)
    outsider = _student(api_client, "outsider@example.com")
    course = _create_course(api_client, teacher["access_token"])
    api_client.post(
        f"{COURSES_URL}/{course['id']}/archive", headers=_auth(teacher["access_token"])
    )

    update = api_client.patch(
        f"{COURSES_URL}/{course['id']}",
        json={"name": "改名"},
        headers=_auth(outsider["access_token"]),
    )
    reset = api_client.post(
        f"{COURSES_URL}/{course['id']}/invite-code",
        headers=_auth(outsider["access_token"]),
    )

    for response in (update, reset):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "COURSE_FORBIDDEN"


def test_course_error_request_id_matches_header(api_client) -> None:
    teacher = _teacher(api_client)

    response = api_client.get(
        f"{COURSES_URL}/{uuid.uuid4()}",
        headers={**_auth(teacher["access_token"]), "X-Request-ID": "course-trace-7"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == "course-trace-7"
    assert response.headers["x-request-id"] == "course-trace-7"


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #
async def test_concurrent_join_creates_only_one_member(
    async_api_client: httpx.AsyncClient, pg_sync_engine
) -> None:
    """并发重复加入：只有一个 201，其余 200，成员记录唯一。"""
    teacher = await _a_teacher(async_api_client)
    student = await _a_student(async_api_client, "student@example.com")
    course = await _a_create_course(async_api_client, teacher["access_token"])
    headers = _auth(student["access_token"])

    responses = await asyncio.gather(
        *(
            async_api_client.post(
                f"{COURSES_URL}/join",
                json={"invite_code": course["invite_code"]},
                headers=headers,
            )
            for _ in range(6)
        )
    )

    statuses = [response.status_code for response in responses]
    # 注意：201 > 200，不能用 sorted()[0] 判断"首次加入"
    assert statuses.count(201) == 1, f"应恰好一次首次加入，实际 {statuses}"
    assert statuses.count(200) == len(responses) - 1, f"其余应为幂等成功，实际 {statuses}"

    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM course_members WHERE course_id = :cid"),
            {"cid": uuid.UUID(course["id"])},
        ).scalar_one()
    assert count == 2, "成员表只有创建教师与该学生两条记录"


async def test_concurrent_reset_keeps_one_valid_code(
    async_api_client: httpx.AsyncClient, pg_sync_engine
) -> None:
    """并发重置邀请码：请求按事务顺序生效，最终数据库里只有一个有效码。"""
    teacher = await _a_teacher(async_api_client)
    course = await _a_create_course(async_api_client, teacher["access_token"])
    headers = _auth(teacher["access_token"])
    course_id = uuid.UUID(course["id"])

    responses = await asyncio.gather(
        *(
            async_api_client.post(
                f"{COURSES_URL}/{course['id']}/invite-code", headers=headers
            )
            for _ in range(6)
        )
    )

    for response in responses:
        assert response.status_code == 200

    with pg_sync_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT invite_code FROM courses WHERE id = :cid"),
            {"cid": course_id},
        ).scalar_one()

    issued = {response.json()["invite_code"] for response in responses}
    assert stored in issued, "数据库里的邀请码应是最后一次生效的那个"
    # 只有当前存储的码可用于加入
    student = await _a_student(async_api_client, "student@example.com")
    join = await async_api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": stored},
        headers=_auth(student["access_token"]),
    )
    assert join.status_code == 201


async def test_join_rechecks_code_after_waiting_for_reset(
    async_api_client: httpx.AsyncClient, monkeypatch
) -> None:
    """加入在锁前读到旧码时，轮换完成后仍须拒绝旧码。"""
    teacher = await _a_teacher(async_api_client)
    student = await _a_student(async_api_client, "student@example.com")
    course = await _a_create_course(async_api_client, teacher["access_token"])
    before_lock = asyncio.Event()
    resume_join = asyncio.Event()
    original = course_repo.get_course_for_update
    first_call = True

    async def pause_first_lock(session, course_id):
        nonlocal first_call
        if first_call:
            first_call = False
            before_lock.set()
            await resume_join.wait()
        return await original(session, course_id)

    monkeypatch.setattr(course_repo, "get_course_for_update", pause_first_lock)
    join_task = asyncio.create_task(
        async_api_client.post(
            f"{COURSES_URL}/join",
            json={"invite_code": course["invite_code"]},
            headers=_auth(student["access_token"]),
        )
    )
    try:
        await asyncio.wait_for(before_lock.wait(), 10)
        reset = await async_api_client.post(
            f"{COURSES_URL}/{course['id']}/invite-code",
            headers=_auth(teacher["access_token"]),
        )
        assert reset.status_code == 200, reset.text
    finally:
        resume_join.set()
    joined = await asyncio.wait_for(join_task, 10)

    assert joined.status_code == 422, joined.text
    assert joined.json()["error"]["code"] == "INVITE_CODE_INVALID"


async def test_join_rechecks_status_after_waiting_for_archive(
    async_api_client: httpx.AsyncClient, monkeypatch
) -> None:
    """加入在锁前读到活动状态时，归档完成后须返回 409。"""
    teacher = await _a_teacher(async_api_client)
    student = await _a_student(async_api_client, "student@example.com")
    course = await _a_create_course(async_api_client, teacher["access_token"])
    before_lock = asyncio.Event()
    resume_join = asyncio.Event()
    original = course_repo.get_course_for_update
    first_call = True

    async def pause_first_lock(session, course_id):
        nonlocal first_call
        if first_call:
            first_call = False
            before_lock.set()
            await resume_join.wait()
        return await original(session, course_id)

    monkeypatch.setattr(course_repo, "get_course_for_update", pause_first_lock)
    join_task = asyncio.create_task(
        async_api_client.post(
            f"{COURSES_URL}/join",
            json={"invite_code": course["invite_code"]},
            headers=_auth(student["access_token"]),
        )
    )
    try:
        await asyncio.wait_for(before_lock.wait(), 10)
        archived = await async_api_client.post(
            f"{COURSES_URL}/{course['id']}/archive",
            headers=_auth(teacher["access_token"]),
        )
        assert archived.status_code == 200, archived.text
    finally:
        resume_join.set()
    joined = await asyncio.wait_for(join_task, 10)

    assert joined.status_code == 409, joined.text
    assert joined.json()["error"]["code"] == "COURSE_ARCHIVED"


async def test_update_rechecks_status_after_waiting_for_archive(
    async_api_client: httpx.AsyncClient, monkeypatch
) -> None:
    """修改与归档锁同一课程行，归档先完成时修改必须被拒绝。"""
    teacher = await _a_teacher(async_api_client)
    course = await _a_create_course(async_api_client, teacher["access_token"])
    before_lock = asyncio.Event()
    resume_update = asyncio.Event()
    original = course_repo.get_course_for_update
    first_call = True

    async def pause_first_lock(session, course_id):
        nonlocal first_call
        if first_call:
            first_call = False
            before_lock.set()
            await resume_update.wait()
        return await original(session, course_id)

    monkeypatch.setattr(course_repo, "get_course_for_update", pause_first_lock)
    update_task = asyncio.create_task(
        async_api_client.patch(
            f"{COURSES_URL}/{course['id']}",
            json={"name": "不应写入"},
            headers=_auth(teacher["access_token"]),
        )
    )
    try:
        await asyncio.wait_for(before_lock.wait(), 10)
        archived = await async_api_client.post(
            f"{COURSES_URL}/{course['id']}/archive",
            headers=_auth(teacher["access_token"]),
        )
        assert archived.status_code == 200, archived.text
    finally:
        resume_update.set()
    updated = await asyncio.wait_for(update_task, 10)

    assert updated.status_code == 409, updated.text
    assert updated.json()["error"]["code"] == "COURSE_ARCHIVED"
    detail = await async_api_client.get(
        f"{COURSES_URL}/{course['id']}", headers=_auth(teacher["access_token"])
    )
    assert detail.status_code == 200
    assert detail.json()["name"] == course["name"]
