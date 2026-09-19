"""课程接口契约测试（``docs/api-contract.md`` 第 3 节）。

核对成功状态码、统一错误结构、邀请码字段可见性、Bearer 要求，
以及导出的 OpenAPI 是否如实描述这些行为。

邀请码可见性是本节重点：列表、加入响应、学生详情与归档详情都 **不出现**
``invite_code`` 字段（缺失而非 ``null``）；只有创建教师查看未归档课程时才有。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.core.error_codes import ErrorCode
from app.main import create_app

PASSWORD = "Demo password 2026!"
COURSES_URL = "/api/v1/courses"

#: 导出的 OpenAPI（与 backend/scripts/export_openapi.py 一致）
OPENAPI_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.json"
)

#: 统一错误结构的键
ENVELOPE_KEYS = {"code", "message", "details", "request_id"}


def _register(client, email: str, role: str, name: str) -> None:
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
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup(api_client) -> tuple[str, str, dict]:
    """准备一名教师、一名学生和一门课程，返回 (教师令牌, 学生令牌, 课程详情)。"""
    _register(api_client, "teacher@example.com", "TEACHER", "张老师")
    _register(api_client, "student@example.com", "STUDENT", "李同学")
    teacher_token = _login(api_client, "teacher@example.com")
    student_token = _login(api_client, "student@example.com")

    course = api_client.post(
        COURSES_URL,
        json={"name": "软件工程实验", "description": "课程说明"},
        headers=_auth(teacher_token),
    ).json()
    return teacher_token, student_token, course


# --------------------------------------------------------------------------- #
# 成功响应的状态码与结构
# --------------------------------------------------------------------------- #
def test_success_status_codes_and_shapes(api_client) -> None:
    teacher_token, student_token, course = _setup(api_client)
    course_id = course["id"]

    created = api_client.post(
        COURSES_URL,
        json={"name": "契约课程"},
        headers=_auth(teacher_token),
    )
    listed = api_client.get(COURSES_URL, headers=_auth(teacher_token))
    detail = api_client.get(f"{COURSES_URL}/{course_id}", headers=_auth(teacher_token))
    updated = api_client.patch(
        f"{COURSES_URL}/{course_id}", json={"name": "改名"}, headers=_auth(teacher_token)
    )
    joined = api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student_token),
    )
    members = api_client.get(
        f"{COURSES_URL}/{course_id}/members", headers=_auth(teacher_token)
    )
    archived = api_client.post(
        f"{COURSES_URL}/{course_id}/archive", headers=_auth(teacher_token)
    )
    reset = api_client.post(
        f"{COURSES_URL}/{course_id}/invite-code", headers=_auth(teacher_token)
    )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert detail.status_code == 200
    assert updated.status_code == 200
    assert joined.status_code == 201
    assert members.status_code == 200
    # 归档后重置邀请码会被拒绝；这里先验证归档与重置各自的状态码
    assert archived.status_code == 200
    assert reset.status_code == 409, "归档课程重置邀请码应返回 409"

    # 分页结构
    page = listed.json()
    assert set(page) == {"items", "page", "page_size", "total"}
    assert isinstance(page["items"], list)

    # 重置邀请码响应只有 invite_code
    fresh_course = api_client.post(
        COURSES_URL, json={"name": "重置用课程"}, headers=_auth(teacher_token)
    ).json()
    reset_ok = api_client.post(
        f"{COURSES_URL}/{fresh_course['id']}/invite-code", headers=_auth(teacher_token)
    )
    assert reset_ok.status_code == 200
    assert set(reset_ok.json()) == {"invite_code"}


def test_error_responses_share_the_envelope(api_client) -> None:
    teacher_token, student_token, course = _setup(api_client)
    course_id = course["id"]

    # 旁听生：已登录但不是课程成员，用于验证 403 COURSE_FORBIDDEN
    _register(api_client, "other@example.com", "STUDENT", "旁听生")
    other_token = _login(api_client, "other@example.com")

    samples = {
        401: api_client.get(f"{COURSES_URL}/{course_id}"),
        403: api_client.get(f"{COURSES_URL}/{course_id}", headers=_auth(other_token)),
        404: api_client.get(
            f"{COURSES_URL}/{uuid.uuid4()}", headers=_auth(teacher_token)
        ),
        422: api_client.post(
            f"{COURSES_URL}/join",
            json={"invite_code": "BADCODE00001"},
            headers=_auth(student_token),
        ),
    }

    for expected_status, response in samples.items():
        assert response.status_code == expected_status, response.text
        error = response.json()["error"]
        assert set(error) == ENVELOPE_KEYS, f"{expected_status} 结构不一致"
        assert isinstance(error["details"], dict)
        assert error["code"]


def test_archived_conflict_uses_course_archived_code(api_client) -> None:
    teacher_token, _, course = _setup(api_client)
    course_id = course["id"]
    api_client.post(f"{COURSES_URL}/{course_id}/archive", headers=_auth(teacher_token))

    response = api_client.patch(
        f"{COURSES_URL}/{course_id}", json={"name": "改名"}, headers=_auth(teacher_token)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 邀请码字段可见性
# --------------------------------------------------------------------------- #
def test_invite_code_visibility(api_client) -> None:
    teacher_token, student_token, course = _setup(api_client)
    course_id = course["id"]
    api_client.post(
        f"{COURSES_URL}/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student_token),
    )

    creator_detail = api_client.get(
        f"{COURSES_URL}/{course_id}", headers=_auth(teacher_token)
    ).json()
    student_detail = api_client.get(
        f"{COURSES_URL}/{course_id}", headers=_auth(student_token)
    ).json()
    listed = api_client.get(COURSES_URL, headers=_auth(teacher_token)).json()

    assert creator_detail["invite_code"] == course["invite_code"]
    assert "invite_code" not in student_detail
    assert "invite_code" not in listed["items"][0]

    # 归档后连创建教师也不再返回邀请码
    api_client.post(f"{COURSES_URL}/{course_id}/archive", headers=_auth(teacher_token))
    archived_detail = api_client.get(
        f"{COURSES_URL}/{course_id}", headers=_auth(teacher_token)
    ).json()
    assert "invite_code" not in archived_detail
    assert "null" not in json.dumps(archived_detail, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Bearer 要求
# --------------------------------------------------------------------------- #
def test_all_course_endpoints_require_bearer(api_client) -> None:
    course_id = str(uuid.uuid4())

    responses = [
        api_client.post(COURSES_URL, json={"name": "课程"}),
        api_client.get(COURSES_URL),
        api_client.get(f"{COURSES_URL}/{course_id}"),
        api_client.patch(f"{COURSES_URL}/{course_id}", json={"name": "课程"}),
        api_client.post(f"{COURSES_URL}/{course_id}/archive"),
        api_client.post(f"{COURSES_URL}/{course_id}/invite-code"),
        api_client.post(f"{COURSES_URL}/join", json={"invite_code": "AB12CD34EF56"}),
        api_client.get(f"{COURSES_URL}/{course_id}/members"),
    ]

    for response in responses:
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


# --------------------------------------------------------------------------- #
# 导出的 OpenAPI
# --------------------------------------------------------------------------- #
def test_openapi_describes_courses() -> None:
    schema: dict = create_app().openapi()
    paths = schema["paths"]

    for path in (
        "/api/v1/courses",
        "/api/v1/courses/join",
        "/api/v1/courses/{course_id}",
        "/api/v1/courses/{course_id}/archive",
        "/api/v1/courses/{course_id}/invite-code",
        "/api/v1/courses/{course_id}/members",
    ):
        assert path in paths, f"{path} 未出现在 OpenAPI 中"

    # 加入接口同时声明 201 与 200
    assert {"201", "200"} <= set(paths["/api/v1/courses/join"]["post"]["responses"])
    # 归档冲突在所有写入接口上有声明
    assert "409" in paths["/api/v1/courses/{course_id}/invite-code"]["post"]["responses"]

    components = schema["components"]["schemas"]
    assert "invite_code" not in components["CourseSummary"]["properties"]
    assert "invite_code" not in components["CourseDetail"]["properties"]
    teacher_detail = components["CourseDetailWithInviteCode"]
    assert teacher_detail["properties"]["invite_code"]["type"] == "string"
    assert "invite_code" in teacher_detail["required"]
    assert "anyOf" not in teacher_detail["properties"]["invite_code"]
    detail_response = paths["/api/v1/courses/{course_id}"]["get"]["responses"]["200"]
    detail_schema = detail_response["content"]["application/json"]["schema"]
    assert {item["$ref"].rsplit("/", 1)[-1] for item in detail_schema["anyOf"]} == {
        "CourseDetail", "CourseDetailWithInviteCode"
    }
    for method, path, model in (
        ("post", "/api/v1/courses", "CourseDetailWithInviteCode"),
        ("patch", "/api/v1/courses/{course_id}", "CourseDetailWithInviteCode"),
        ("post", "/api/v1/courses/{course_id}/archive", "CourseDetail"),
        ("post", "/api/v1/courses/join", "CourseSummary"),
    ):
        success = "201" if path == "/api/v1/courses" else "200"
        if path == "/api/v1/courses/join":
            success = "201"
        response_schema = paths[path][method]["responses"][success]["content"]["application/json"]["schema"]
        assert response_schema["$ref"].endswith(f"/{model}")
    assert "CourseMemberSummary" in components
    assert "email" not in components["CourseMemberSummary"]["properties"]


def test_exported_openapi_includes_courses_and_new_error_code() -> None:
    """导出物里必须能找到课程接口与新增的 COURSE_ARCHIVED。

    "导出物与代码完全一致"由 test_openapi_contract.py 统一守卫，这里只核对内容。
    """
    exported = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    assert "/api/v1/courses" in exported["paths"]
    assert "COURSE_ARCHIVED" in exported["components"]["schemas"]["ErrorCode"]["enum"]
    assert ErrorCode.COURSE_ARCHIVED.value == "COURSE_ARCHIVED"
