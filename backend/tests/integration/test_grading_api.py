"""提交与批改接口的 PostgreSQL 集成测试（``docs/api-contract.md`` 第 9 节）。

覆盖：
- 上传初始化/完成/幂等/截止边界/重复初始化与「只能提交一份」；
- 列表的角色化行为（教师全班分页、学生仅本人）与分页包装；
- 详情权限、下载地址、固定评分版本；
- 触发批改的状态分流与幂等；
- 教师复核的完整快照语义、发布与「发布后不可修改」；
- 学生视角的字段隐藏（发布前 404、发布后 AI 原始建议为 null）。

模型为本地假 HTTP 服务（``httpx.MockTransport``），真实模型效果未验收。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.grading import worker
from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage

PASSWORD = "Demo password 2026!"

PDF_MIME = "application/pdf"

UPLOADS_URL = "/api/v1/assignments/{assignment_id}/submissions/uploads"
COMPLETE_URL = (
    "/api/v1/assignments/{assignment_id}/submissions/uploads/{upload_id}/complete"
)
SUBMISSIONS_URL = "/api/v1/assignments/{assignment_id}/submissions"
DETAIL_URL = "/api/v1/submissions/{submission_id}"
GRADE_URL = "/api/v1/submissions/{submission_id}/grade"
REVIEW_URL = "/api/v1/submissions/{submission_id}/grade-review"
REVIEW_PATCH_URL = "/api/v1/grade-reviews/{review_id}"
REVIEW_PUBLISH_URL = "/api/v1/grade-reviews/{review_id}/publish"


# --------------------------------------------------------------------------- #
# 报告样例：真实可解析的最小 PDF
# --------------------------------------------------------------------------- #
def build_pdf(pages_text: list[str]) -> bytes:
    """手工构造最小有效 PDF（每页一行文本，pypdf 可提取）。"""
    n = len(pages_text)
    kids = " ".join(str(4 + 2 * i) + " 0 R" for i in range(n))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [" + kids + "] /Count " + str(n) + " >>").encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(pages_text):
        page_num = 4 + 2 * i
        content_num = page_num + 1
        stream = ("BT /F1 14 Tf 72 720 Td (" + text + ") Tj ET").encode()
        objects[page_num] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Contents " + str(content_num) + " 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
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
    out += (
        "trailer\n<< /Size " + str(max_num + 1) + " /Root 1 0 R >>\n"
        "startxref\n" + str(xref_pos) + "\n%%EOF"
    ).encode()
    return bytes(out)


#: 报告中可被 pypdf 抽取的原文（证据摘录必须是它的子串）。
#: 刻意使用 ASCII：PDF 字面量字符串按 Latin-1 解码，中文会变成乱码而无法作为证据核对。
REPORT_LINES = [
    "Requirement analysis: login and course management are supported.",
    "Modeling note: the exception flow is not covered yet.",
]
REPORT_BODY = build_pdf(REPORT_LINES)
REPORT_SHA256 = hashlib.sha256(REPORT_BODY).hexdigest()
REPORT_SIZE = len(REPORT_BODY)


def fake_model_response() -> httpx.MockTransport:
    """假模型服务：对两项评分规则各返回一条带真实证据与位置的批改结果。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        prompt = body["messages"][-1]["content"]
        items = []
        for line in prompt.splitlines():
            if not line.startswith("- rubric_item_id="):
                continue
            item_id = line.split("=", 1)[1].split("｜")[0]
            items.append(
                {
                    "rubric_item_id": item_id,
                    "score": 10,
                    "comment": "覆盖较完整",
                    "evidence_quote": REPORT_LINES[0],
                    "location_start": 1,
                    "location_end": 1,
                    "error_type": "",
                    "improvement_suggestion": "补充异常流程",
                }
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "整体完成度较好，异常流程需要补充。",
                                    "items": items,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _register(client: TestClient, email: str, role: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": email.split("@")[0],
            "role": role,
        },
    )
    assert response.status_code == 201, response.text


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _course_with_members(client: TestClient) -> tuple[str, str, str]:
    """建课程并让一名学生加入，返回 ``(课程 ID, 教师令牌, 学生令牌)``。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    created = client.post(
        "/api/v1/courses",
        json={"name": "实验课", "description": ""},
        headers=_auth(teacher),
    )
    assert created.status_code == 201, created.text
    course = created.json()

    _register(client, "student@example.com", "STUDENT")
    student = _login(client, "student@example.com")
    joined = client.post(
        "/api/v1/courses/join",
        json={"invite_code": course["invite_code"]},
        headers=_auth(student),
    )
    assert joined.status_code in (200, 201), joined.text
    return course["id"], teacher, student


def _create_assignment(
    client: TestClient,
    token: str,
    course_id: str,
    *,
    due_at: str | None = None,
    allow_late: bool = False,
    total: float = 100,
    items: list[dict] | None = None,
) -> dict:
    payload = {
        "title": "实验一 需求分析",
        "description": "请提交实验报告",
        "total_score": total,
        "allow_late_submission": allow_late,
        "rubric_items": items
        or [
            {"title": "需求完整性", "description": "", "max_score": 60, "order": 1},
            {"title": "建模规范", "description": "", "max_score": 40, "order": 2},
        ],
    }
    if due_at is not None:
        payload["due_at"] = due_at
    response = client.post(
        f"/api/v1/courses/{course_id}/assignments",
        json=payload,
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish_assignment(client: TestClient, token: str, assignment_id: str) -> None:
    response = client.post(
        f"/api/v1/assignments/{assignment_id}/publish", headers=_auth(token)
    )
    assert response.status_code == 200, response.text


def _open_assignment(client: TestClient) -> tuple[str, str, str, dict]:
    """准备一个已发布、无截止时间的任务，返回 (课程, 教师, 学生, 任务)。"""
    course_id, teacher, student = _course_with_members(client)
    assignment = _create_assignment(client, teacher, course_id)
    _publish_assignment(client, teacher, assignment["id"])
    return course_id, teacher, student, assignment


def _init_upload(
    client: TestClient,
    token: str,
    assignment_id: str,
    *,
    filename: str = "report.pdf",
    body: bytes = REPORT_BODY,
    content_type: str = PDF_MIME,
) -> dict:
    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment_id),
        json={
            "filename": filename,
            "content_type": content_type,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _simulate_put(
    client: TestClient,
    fake: FakeStorage,
    init: dict,
    *,
    body: bytes = REPORT_BODY,
    content_type: str = PDF_MIME,
) -> str:
    """模拟浏览器直传：按预签名地址对应的对象键写入假存储。"""
    upload_url = init["upload_url"]
    keys = [key for key, url in fake.presigned_urls.items() if url == upload_url]
    assert keys, "未找到预签名地址对应的对象键"
    object_key = keys[0]
    fake.store_object(
        object_key,
        size=len(body),
        content_type=content_type,
        sha256_hex=hashlib.sha256(body).hexdigest(),
        content=body,
    )
    return object_key


def _complete(client: TestClient, token: str, assignment_id: str, upload_id: str):
    return client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=upload_id),
        headers=_auth(token),
    )


def _submit_report(
    client: TestClient,
    fake: FakeStorage,
    token: str,
    assignment_id: str,
    *,
    body: bytes = REPORT_BODY,
    filename: str = "report.pdf",
    content_type: str = PDF_MIME,
) -> dict:
    """完成一次「初始化 → 直传 → 完成提交」，返回提交详情。"""
    init = _init_upload(
        client, token, assignment_id, body=body, filename=filename, content_type=content_type
    )
    _simulate_put(client, fake, init, body=body, content_type=content_type)
    completed = _complete(client, token, assignment_id, init["upload_id"])
    assert completed.status_code == 201, completed.text
    return completed.json()


def _run_worker(
    pg_session_factory,
    settings: Settings,
    storage: FakeStorage,
    *,
    max_jobs: int = 1,
) -> int:
    """同步驱动一次批改 Worker 批次（假模型 + 假存储）。"""
    return asyncio.run(
        worker.run_pending_batch(
            pg_session_factory,
            settings=settings,
            storage=storage,
            ai_client_factory=lambda: httpx.Client(
                transport=fake_model_response(), timeout=10.0
            ),
            max_jobs=max_jobs,
        )
    )


def _grading_settings(make_settings) -> Settings:
    return make_settings(
        ai_base_url="http://model.invalid/v1",
        ai_model="fake-grade-model",
    )


def _submission_id_of(client: TestClient, token: str, assignment_id: str) -> str:
    response = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment_id), headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    return items[0]["id"]


# --------------------------------------------------------------------------- #
# 9.2 初始化上传
# --------------------------------------------------------------------------- #
def test_init_upload_returns_presigned_put(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)

    body = _init_upload(client, student, assignment["id"])

    assert body["method"] == "PUT"
    assert body["upload_url"]
    assert body["headers"]["Content-Type"] == PDF_MIME
    assert body["headers"]["If-None-Match"] == "*"
    assert body["submission_id"] and body["upload_id"]
    assert body["expires_at"] and body["confirm_deadline_at"]


def test_init_upload_rejects_doc_legacy_format(client, fake_storage) -> None:
    """契约 9.2：旧版 .doc 必须被拒绝。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment["id"]),
        json={
            "filename": "report.doc",
            "content_type": "application/msword",
            "size": 10,
            "sha256": "a" * 64,
        },
        headers=_auth(student),
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "UPLOAD_INVALID"
    assert error["details"]["reason"] == "FILE_TYPE_NOT_ALLOWED"


def test_init_upload_requires_published_assignment(client, fake_storage) -> None:
    course_id, teacher, student = _course_with_members(client)
    draft = _create_assignment(client, teacher, course_id)

    response = client.post(
        UPLOADS_URL.format(assignment_id=draft["id"]),
        json={
            "filename": "report.pdf",
            "content_type": PDF_MIME,
            "size": REPORT_SIZE,
            "sha256": REPORT_SHA256,
        },
        headers=_auth(student),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"


def test_init_upload_rejects_teacher_role(client, fake_storage) -> None:
    """上传仅限学生：课程创建教师调用返回 403 ROLE_FORBIDDEN。"""
    _course, teacher, _student, assignment = _open_assignment(client)

    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment["id"]),
        json={
            "filename": "report.pdf",
            "content_type": PDF_MIME,
            "size": REPORT_SIZE,
            "sha256": REPORT_SHA256,
        },
        headers=_auth(teacher),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_reinit_upload_reuses_uploading_submission(client, fake_storage) -> None:
    """重新初始化复用同一份 `UPLOADING` 提交，并签发新的上传会话。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    first = _init_upload(client, student, assignment["id"])
    second = _init_upload(client, student, assignment["id"])

    assert first["submission_id"] == second["submission_id"]
    assert first["upload_id"] != second["upload_id"]


# --------------------------------------------------------------------------- #
# 9.3 完成提交
# --------------------------------------------------------------------------- #
def test_complete_upload_creates_submission_and_fixes_rubric_version(
    client, fake_storage
) -> None:
    _course, teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    completed = _complete(client, student, assignment["id"], init["upload_id"])

    assert completed.status_code == 201, completed.text
    detail = completed.json()
    assert detail["status"] == "SUBMITTED"
    assert detail["is_late"] is False
    assert detail["rubric_version"] == 1
    assert detail["submitted_at"]
    assert detail["download_url"]  # 完成响应现场签发下载地址

    # 教师修改 Rubric 只影响后续提交：已提交记录仍固定在版本 1
    updated = client.patch(
        f"/api/v1/assignments/{assignment['id']}",
        json={
            "rubric_items": [
                {"title": "需求完整性", "description": "", "max_score": 50, "order": 1},
                {"title": "建模规范", "description": "", "max_score": 50, "order": 2},
            ]
        },
        headers=_auth(teacher),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rubric_version"] == 2

    detail_after = client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(student)
    )
    assert detail_after.status_code == 200
    assert detail_after.json()["rubric_version"] == 1


def test_complete_upload_is_idempotent_for_same_upload(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    first = _complete(client, student, assignment["id"], init["upload_id"])
    second = _complete(client, student, assignment["id"], init["upload_id"])

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["submitted_at"] == first.json()["submitted_at"]


def test_reinit_after_submission_returns_already_exists(client, fake_storage) -> None:
    """契约 9.2：已有正式提交时再次初始化返回 409。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    _submit_report(client, fake_storage, student, assignment["id"])

    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment["id"]),
        json={
            "filename": "report.pdf",
            "content_type": PDF_MIME,
            "size": REPORT_SIZE,
            "sha256": REPORT_SHA256,
        },
        headers=_auth(student),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBMISSION_ALREADY_EXISTS"


def test_complete_upload_rejects_missing_object(client, fake_storage) -> None:
    """未直传就完成：422 UPLOAD_INVALID（reason=OBJECT_MISSING）。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "UPLOAD_INVALID"
    assert error["details"]["reason"] == "OBJECT_MISSING"


def test_complete_upload_rejects_tampered_size(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)
    fake_storage.objects[object_key].size += 1

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "OBJECT_SIZE_MISMATCH"


def test_complete_upload_marks_late_submission(client, fake_storage) -> None:
    """截止时间已过但允许补交：`is_late` 为 true。"""
    course_id, teacher, student = _course_with_members(client)
    due_at = (utc_now() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assignment = _create_assignment(
        client, teacher, course_id, due_at=due_at, allow_late=True
    )
    _publish_assignment(client, teacher, assignment["id"])

    detail = _submit_report(client, fake_storage, student, assignment["id"])

    assert detail["is_late"] is True


def test_complete_upload_rejects_closed_assignment(client, fake_storage) -> None:
    """任务在直传期间被关闭：完成提交返回 409。"""
    _course, teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)
    closed = client.post(
        f"/api/v1/assignments/{assignment['id']}/close", headers=_auth(teacher)
    )
    assert closed.status_code == 200, closed.text

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"


# --------------------------------------------------------------------------- #
# 9.4 / 9.5 列表与详情
# --------------------------------------------------------------------------- #
def test_teacher_list_paginates_all_submissions_and_hides_uploading(
    client, fake_storage
) -> None:
    course_id, teacher, student = _course_with_members(client)
    assignment = _create_assignment(client, teacher, course_id)
    _publish_assignment(client, teacher, assignment["id"])
    _submit_report(client, fake_storage, student, assignment["id"])

    # 另一位学生只初始化、未完成：不得进入教师正式列表
    _register(client, "student2@example.com", "STUDENT")
    other = _login(client, "student2@example.com")
    joined = client.post(
        "/api/v1/courses/join",
        json={"invite_code": client.get(
            f"/api/v1/courses/{course_id}", headers=_auth(teacher)
        ).json()["invite_code"]},
        headers=_auth(other),
    )
    assert joined.status_code in (200, 201)
    _init_upload(client, other, assignment["id"])

    response = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]),
        headers=_auth(teacher),
        params={"page": 1, "page_size": 20},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["total"] == 1
    assert [item["student_id"] for item in body["items"]]


def test_student_list_returns_only_own_submission(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)

    empty = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]), headers=_auth(student)
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "page": 1, "page_size": 20, "total": 0}

    _submit_report(client, fake_storage, student, assignment["id"])

    after = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]), headers=_auth(student)
    )
    assert after.status_code == 200
    assert after.json()["total"] == 1


def test_non_member_teacher_gets_404(client, fake_storage) -> None:
    """未加入课程的其他教师：不做成员鉴别，统一 404（契约 9.1）。"""
    _course, _teacher, _student, assignment = _open_assignment(client)
    _register(client, "teacher2@example.com", "TEACHER")
    other = _login(client, "teacher2@example.com")

    response = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]), headers=_auth(other)
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_member_teacher_without_creator_role_is_rejected(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """防御性分支：已是课程成员但不是创建教师时的 403。

    当前第 3 节只允许学生加入课程，正常 API 流程产生不了这种成员记录，
    因此直接写入一条 ``course_role = TEACHER`` 的成员行来锁定契约 9.1 的行为：
    列表 ``403 ROLE_FORBIDDEN``、详情 ``403 COURSE_FORBIDDEN``。
    """
    course_id, teacher, _student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, _student, assignment["id"])

    _register(client, "teacher2@example.com", "TEACHER")
    other = _login(client, "teacher2@example.com")
    with pg_sync_engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM users WHERE email_normalized = :email"),
            {"email": "teacher2@example.com"},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO course_members"
                " (id, course_id, user_id, course_role, joined_at)"
                " VALUES (CAST(:id AS uuid), CAST(:course_id AS uuid),"
                " CAST(:user_id AS uuid), 'TEACHER', now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "course_id": course_id,
                "user_id": str(user_id),
            },
        )

    listing = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]), headers=_auth(other)
    )
    assert listing.status_code == 403, listing.text
    assert listing.json()["error"]["code"] == "ROLE_FORBIDDEN"

    single = client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(other)
    )
    assert single.status_code == 403, single.text
    assert single.json()["error"]["code"] == "COURSE_FORBIDDEN"

    # 创建教师不受影响
    assert client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    ).status_code == 200


def test_non_member_gets_404(client, fake_storage) -> None:
    _course, _teacher, _student, assignment = _open_assignment(client)
    _register(client, "outsider@example.com", "STUDENT")
    outsider = _login(client, "outsider@example.com")

    response = client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]),
        headers=_auth(outsider),
    )

    assert response.status_code == 404


def test_other_student_cannot_read_submission(client, fake_storage) -> None:
    course_id, teacher, student = _course_with_members(client)
    assignment = _create_assignment(client, teacher, course_id)
    _publish_assignment(client, teacher, assignment["id"])
    detail = _submit_report(client, fake_storage, student, assignment["id"])

    _register(client, "student2@example.com", "STUDENT")
    other = _login(client, "student2@example.com")
    client.post(
        "/api/v1/courses/join",
        json={"invite_code": client.get(
            f"/api/v1/courses/{course_id}", headers=_auth(teacher)
        ).json()["invite_code"]},
        headers=_auth(other),
    )

    response = client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(other)
    )

    assert response.status_code == 404, response.text


def test_detail_returns_fresh_download_url(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])

    response = client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(student)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["download_url"]
    assert body["download_expires_at"]
    assert body["sha256"] == REPORT_SHA256


# --------------------------------------------------------------------------- #
# 9.6 触发批改
# --------------------------------------------------------------------------- #
def test_grade_requires_creator_teacher(client, fake_storage) -> None:
    _course, _teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])

    response = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(student)
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_grade_rejects_uploading_submission(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """未完成提交（`UPLOADING`，不在学生列表中）触发批改返回 409。"""
    _course, teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])

    with pg_sync_engine.connect() as connection:
        submission_id = connection.execute(
            text(
                "SELECT id FROM submissions"
                " WHERE assignment_id = CAST(:id AS uuid)"
            ),
            {"id": assignment["id"]},
        ).scalar_one()

    response = client.post(
        GRADE_URL.format(submission_id=str(submission_id)), headers=_auth(teacher)
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBMISSION_NOT_READY"
    assert init["upload_id"]


def test_grade_creates_job_and_is_idempotent(client, fake_storage) -> None:
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])

    first = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    second = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["type"] == "SUBMISSION_GRADE"
    assert first.json()["resource_type"] == "SUBMISSION"
    assert second.json()["id"] == first.json()["id"]


def test_grade_rejects_review_required_submission(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))
    assert _run_worker(
        pg_session_factory, _grading_settings(make_settings), fake_storage
    ) == 1

    response = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBMISSION_NOT_READY"


def test_job_visibility_for_submission_grade(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """`GET /jobs/{id}` 对批改任务按提交可见性返回（教师 + 提交本人）。"""
    course_id, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    job = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    ).json()

    assert client.get(
        f"/api/v1/jobs/{job['id']}", headers=_auth(teacher)
    ).status_code == 200
    assert client.get(
        f"/api/v1/jobs/{job['id']}", headers=_auth(student)
    ).status_code == 200

    _register(client, "outsider@example.com", "STUDENT")
    outsider = _login(client, "outsider@example.com")
    assert client.get(
        f"/api/v1/jobs/{job['id']}", headers=_auth(outsider)
    ).status_code == 404
    assert course_id


def test_job_retry_reuses_submission_grade_job(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """`POST /jobs/{id}/retry` 与触发接口共用逻辑：失败任务被重置为 PENDING。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    job = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    ).json()

    # 让它失败：批改时把存储设为不可用
    fake_storage.as_unavailable()
    assert _run_worker(
        pg_session_factory, _grading_settings(make_settings), fake_storage
    ) == 1
    fake_storage.as_available()

    failed = client.get(f"/api/v1/jobs/{job['id']}", headers=_auth(teacher)).json()
    assert failed["status"] == "FAILED"

    retried = client.post(
        f"/api/v1/jobs/{job['id']}/retry", headers=_auth(teacher)
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "PENDING"
    assert retried.json()["id"] == job["id"]


# --------------------------------------------------------------------------- #
# 9.7 / 9.8 / 9.9 批改详情、复核与发布
# --------------------------------------------------------------------------- #
def _graded_review(
    client, fake_storage, pg_session_factory, make_settings
) -> tuple[dict, str, str, str, dict]:
    """跑完整链路到 `REVIEW_REQUIRED`，返回 (任务, 教师, 学生, 提交 ID, 批改)。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))
    assert _run_worker(
        pg_session_factory, _grading_settings(make_settings), fake_storage
    ) == 1

    review = client.get(
        REVIEW_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert review.status_code == 200, review.text
    return assignment, teacher, student, detail["id"], review.json()


def test_worker_produces_review_required_with_ai_suggestions(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, _teacher, _student, submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )

    assert review["reviewed_at"] is None
    assert review["final_total_score"] == 20.0
    assert review["suggested_total_score"] == 20.0
    assert len(review["items"]) == 2
    assert all(item["ai_score"] == item["final_score"] for item in review["items"])
    # 证据定位：来源类型、位置区间与摘录逐字段一致（PDF → 页码）
    for item in review["items"]:
        assert item["evidence_quote"] == REPORT_LINES[0]
        assert item["evidence_source_type"] == "PDF_PAGE"
        assert item["evidence_location_start"] == 1
        assert item["evidence_location_end"] == 1
    assert submission_id


def test_review_detail_hidden_from_student_before_publish(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, teacher, student, submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )

    hidden = client.get(
        REVIEW_URL.format(submission_id=submission_id), headers=_auth(student)
    )
    assert hidden.status_code == 404

    published = client.post(
        REVIEW_PUBLISH_URL.format(review_id=review["id"]),
        headers=_auth(teacher),
    )
    # 未复核就发布：409
    assert published.status_code == 409, published.text
    assert published.json()["error"]["code"] == "GRADE_NOT_REVIEWED"

    # 教师在此之前也不能发布


def test_teacher_review_requires_full_snapshot(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, teacher, _student, _submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )

    missing = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "反馈",
            "items": [
                {
                    "rubric_item_id": review["items"][0]["rubric_item_id"],
                    "final_score": 55,
                }
            ],
        },
        headers=_auth(teacher),
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "反馈",
            "items": [
                {
                    "rubric_item_id": str(uuid.uuid4()),
                    "final_score": 10,
                },
                {
                    "rubric_item_id": review["items"][1]["rubric_item_id"],
                    "final_score": 10,
                },
            ],
        },
        headers=_auth(teacher),
    )
    assert unknown.status_code == 422, unknown.text

    over_max = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "反馈",
            "items": [
                {
                    "rubric_item_id": item["rubric_item_id"],
                    "final_score": item["max_score"] + 1,
                }
                for item in review["items"]
            ],
        },
        headers=_auth(teacher),
    )
    assert over_max.status_code == 422, over_max.text

    too_many_decimals = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "反馈",
            "items": [
                {
                    "rubric_item_id": item["rubric_item_id"],
                    "final_score": 1.005,
                }
                for item in review["items"]
            ],
        },
        headers=_auth(teacher),
    )
    assert too_many_decimals.status_code == 422, too_many_decimals.text


def test_teacher_review_keeps_ai_values_for_audit(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, teacher, student, submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )
    ai_scores = [item["ai_score"] for item in review["items"]]

    saved = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "教师终稿反馈",
            "items": [
                {
                    "rubric_item_id": review["items"][0]["rubric_item_id"],
                    "final_score": 60,
                    "teacher_comment": "需求很完整",
                },
                {
                    "rubric_item_id": review["items"][1]["rubric_item_id"],
                    "final_score": 30,
                    "teacher_comment": "异常流程需要补充",
                },
            ],
        },
        headers=_auth(teacher),
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["teacher_summary"] == "教师终稿反馈"
    assert body["final_total_score"] == 90.0
    assert body["reviewed_at"] is not None
    assert [item["ai_score"] for item in body["items"]] == ai_scores
    assert [item["final_score"] for item in body["items"]] == [60.0, 30.0]

    # 学生此时仍看不到（未发布）
    assert client.get(
        REVIEW_URL.format(submission_id=submission_id), headers=_auth(student)
    ).status_code == 404


def test_publish_makes_review_visible_and_hides_ai_suggestions(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, teacher, student, submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )
    client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "教师终稿反馈",
            "items": [
                {
                    "rubric_item_id": item["rubric_item_id"],
                    "final_score": item["max_score"],
                }
                for item in review["items"]
            ],
        },
        headers=_auth(teacher),
    )

    published = client.post(
        REVIEW_PUBLISH_URL.format(review_id=review["id"]), headers=_auth(teacher)
    )
    assert published.status_code == 200, published.text
    assert published.json()["published_at"]

    # 幂等：不覆盖首次 published_at
    again = client.post(
        REVIEW_PUBLISH_URL.format(review_id=review["id"]), headers=_auth(teacher)
    )
    assert again.status_code == 200
    assert again.json()["published_at"] == published.json()["published_at"]

    student_view = client.get(
        REVIEW_URL.format(submission_id=submission_id), headers=_auth(student)
    )
    assert student_view.status_code == 200, student_view.text
    body = student_view.json()
    assert body["final_total_score"] == 100.0
    assert body["teacher_summary"] == "教师终稿反馈"
    # AI 原始建议对学生隐藏
    assert body["suggested_total_score"] is None
    assert body["ai_summary"] is None
    assert all(item["ai_score"] is None for item in body["items"])
    assert all(item["ai_comment"] is None for item in body["items"])
    # 证据及定位对学生可见（发布后）
    assert all(item["evidence_quote"] for item in body["items"])
    assert all(item["evidence_source_type"] == "PDF_PAGE" for item in body["items"])
    assert all(item["evidence_location_start"] >= 1 for item in body["items"])
    # 终稿与教师评语对学生可见
    assert all(item["final_score"] >= 0 for item in body["items"])


def test_published_review_cannot_be_modified(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    _assignment, teacher, _student, _submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )
    client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "终稿",
            "items": [
                {
                    "rubric_item_id": item["rubric_item_id"],
                    "final_score": item["max_score"],
                }
                for item in review["items"]
            ],
        },
        headers=_auth(teacher),
    )
    client.post(
        REVIEW_PUBLISH_URL.format(review_id=review["id"]), headers=_auth(teacher)
    )

    response = client.patch(
        REVIEW_PATCH_URL.format(review_id=review["id"]),
        json={
            "summary": "再改一次",
            "items": [
                {
                    "rubric_item_id": item["rubric_item_id"],
                    "final_score": 1,
                }
                for item in review["items"]
            ],
        },
        headers=_auth(teacher),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "GRADE_ALREADY_PUBLISHED"


# --------------------------------------------------------------------------- #
# 归档课程
# --------------------------------------------------------------------------- #
def test_archived_course_allows_read_but_blocks_write(client, fake_storage) -> None:
    course_id, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200, archived.text

    # 读历史：列表与详情仍可访问
    assert client.get(
        SUBMISSIONS_URL.format(assignment_id=assignment["id"]), headers=_auth(student)
    ).status_code == 200
    assert client.get(
        DETAIL_URL.format(submission_id=detail["id"]), headers=_auth(student)
    ).status_code == 200

    # 写操作：触发批改返回 409 COURSE_ARCHIVED
    graded = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert graded.status_code == 409, graded.text
    assert graded.json()["error"]["code"] == "COURSE_ARCHIVED"


def test_upload_expired_confirm_window(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """确认窗口已过：完成提交返回 422 UPLOAD_INVALID（reason=UPLOAD_EXPIRED）。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE submission_upload_sessions"
                " SET confirm_deadline_at = now() - interval '1 hour'"
                " WHERE id = CAST(:upload_id AS uuid)"
            ),
            {"upload_id": init["upload_id"]},
        )

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "UPLOAD_EXPIRED"


def test_upload_object_size_can_be_read_back_from_storage(
    client, fake_storage
) -> None:
    """冒烟：假存储确实记录了对象键与大小（其余分支由清理/Worker 用例覆盖）。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)

    assert fake_storage.objects[object_key].size == REPORT_SIZE
    assert b"".join([]) == b""
