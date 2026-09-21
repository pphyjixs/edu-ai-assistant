"""练习写接口的校验优先级、严格类型与请求体边界（契约 7.1 / 7.2 / 7.5 / 7.6 / 10.1）。

固定优先级：**认证 → 资源可见性 → 角色 → 课程归档/资源状态 → 请求体结构与字段
→ 业务写入冲突**。实现方式是把"资源检查 + 行锁"放在依赖里（FastAPI 先解析依赖、
后校验请求体），因此只需断言状态码与错误码，不需要手工解析请求体。

另含：``question_count`` 与作答值的严格类型、发布/重试的空对象请求体边界，
以及"提交响应 = 结果接口 = 数据库"的一致性。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_chat_api import _make_chat_client
from tests.integration.test_practice_api import (
    ATTEMPTS_URL,
    GENERATE_URL,
    PUBLISH_URL,
    RESULT_URL,
    RETRY_URL,
    SET_URL,
    _answers_from_teacher_view,
    _drive_practice_worker,
    _generate,
    _join,
    _ready_course,
    practice_model_factory,
)
from tests.integration.test_materials_api import _auth, _login, _register
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

MALFORMED_BODIES: tuple[tuple[str, bytes], ...] = (
    ("非 JSON", b"not-json"),
    ("截断的 JSON", b'{"question_count":'),
    ("顶层数组", b"[1, 2, 3]"),
    ("非法 UTF-8", b"\xff\xfe\xfa"),
    ("多余字段", json.dumps({"unknown": 1}).encode()),
    ("显式 null 字段", json.dumps({"question_count": None}).encode()),
)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, practice_model_factory()
    ) as test_client:
        yield test_client


def _error(response) -> str:
    return response.json()["error"]["code"]


def _generate_body(material_id: str) -> dict:
    return {
        "material_ids": [material_id],
        "question_count": 3,
        "question_types": ["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"],
        "difficulty": "MEDIUM",
    }


# --------------------------------------------------------------------------- #
# 错误优先级
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", MALFORMED_BODIES, ids=lambda item: item[0])
def test_authentication_precedes_body_validation(
    client: TestClient, fake_storage, case: tuple[str, bytes]
) -> None:
    """未认证时无论请求体如何都返回 401。"""
    course_id = str(uuid.uuid4())
    response = client.post(
        GENERATE_URL.format(course_id=course_id),
        content=case[1],
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401, response.text
    assert _error(response) == "AUTH_TOKEN_EXPIRED"


def test_non_member_404_precedes_body_validation(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """非成员是 404（资源不可见），不会先暴露请求体错误。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-nonmember-{suffix}@example.com",
    )
    outsider_email = f"valid-outsider-{suffix}@example.com"
    _register(client, outsider_email, "TEACHER")
    outsider = _login(client, outsider_email)

    for _label, body in MALFORMED_BODIES:
        response = client.post(
            GENERATE_URL.format(course_id=course_id),
            content=body,
            headers={**_auth(outsider), "Content-Type": "application/json"},
        )
        assert response.status_code == 404, response.text
        assert _error(response) == "RESOURCE_NOT_FOUND"

    # 完全没有请求体同样是 404（资源检查先于请求体）
    empty = client.post(
        GENERATE_URL.format(course_id=course_id), headers=_auth(outsider)
    )
    assert empty.status_code == 404

    # 同一份请求体在合法教师身份下才会暴露请求体错误
    owner = client.post(
        GENERATE_URL.format(course_id=course_id),
        content=b"not-json",
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert owner.status_code == 422
    assert material_id  # 资料已就绪，错误确实来自请求体


def test_role_forbidden_precedes_body_validation(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """学生调用教师接口是 403，先于请求体校验。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-role-{suffix}@example.com",
    )
    student_email = f"valid-role-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    # 生成：学生 + 畸形请求体 → 403
    generate = client.post(
        GENERATE_URL.format(course_id=course_id),
        content=b"not-json",
        headers={**_auth(student), "Content-Type": "application/json"},
    )
    assert generate.status_code == 403, generate.text
    assert _error(generate) == "ROLE_FORBIDDEN"

    # 发布：学生 + 显式 null → 403
    publish = client.post(
        PUBLISH_URL.format(set_id=set_id),
        json=None,
        headers=_auth(student),
    )
    assert publish.status_code == 403, publish.text
    assert _error(publish) == "ROLE_FORBIDDEN"

    # 重试：学生 + 畸形请求体 → 403（任务此时仍为 PENDING，但角色优先）
    retry = client.post(
        RETRY_URL.format(job_id=job_id),
        content=b"[1]",
        headers={**_auth(student), "Content-Type": "application/json"},
    )
    assert retry.status_code == 403, retry.text
    assert _error(retry) == "ROLE_FORBIDDEN"


def test_archived_precedes_body_validation(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """课程已归档是 409，先于请求体校验。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-archived-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    student_email = f"valid-archived-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200

    for _label, body in MALFORMED_BODIES:
        response = client.post(
            GENERATE_URL.format(course_id=course_id),
            content=body,
            headers={**_auth(teacher), "Content-Type": "application/json"},
        )
        assert response.status_code == 409, response.text
        assert _error(response) == "COURSE_ARCHIVED"

    for url in (
        PUBLISH_URL.format(set_id=set_id),
        RETRY_URL.format(job_id=job_id),
    ):
        response = client.post(url, json={"extra": 1}, headers=_auth(teacher))
        assert response.status_code == 409, response.text
        assert _error(response) == "COURSE_ARCHIVED"

    # 归档课程的提交同样是 409（成员身份满足，归档先于请求体）
    submit = client.post(
        ATTEMPTS_URL.format(set_id=set_id),
        content=b"not-json",
        headers={**_auth(student), "Content-Type": "application/json"},
    )
    assert submit.status_code == 409, submit.text
    assert _error(submit) == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 严格类型（HTTP 层）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value", [True, False, "3", "3.0", 3.0, 3.5, 0, -1, 21]
)
def test_question_count_requires_json_integer_over_http(
    client: TestClient, fake_storage, pg_session_factory, make_settings, value: object
) -> None:
    """``question_count`` 只接受 JSON 整数；布尔、字符串、浮点数与越界都是 422。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-count-{suffix}@example.com",
    )
    body = _generate_body(material_id)
    body["question_count"] = value

    response = client.post(
        GENERATE_URL.format(course_id=course_id), json=body, headers=_auth(teacher)
    )

    assert response.status_code == 422, response.text
    assert _error(response) == "VALIDATION_ERROR"


def test_answer_types_are_strict_over_http(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """作答值不接受数字，也不能用字符串冒充判断题布尔值。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-answer-{suffix}@example.com",
    )
    student_email = f"valid-answer-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    _drive_practice_worker(pg_session_factory, make_settings, practice_model_factory())
    client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))

    questions = client.get(
        SET_URL.format(set_id=set_id), headers=_auth(teacher)
    ).json()["questions"]
    answers = _answers_from_teacher_view(questions)
    true_false = next(q for q in questions if q["type"] == "TRUE_FALSE")

    def submit(items: list[dict]) -> object:
        return client.post(
            ATTEMPTS_URL.format(set_id=set_id),
            json={"answers": items},
            headers=_auth(student),
        )

    # 数字答案（0 / 1）不得被转换成布尔值
    for value in (0, 1, 0.0, 1.0):
        bad = [
            {**item, "answer": value} if item["question_id"] == true_false["id"] else item
            for item in answers
        ]
        response = submit(bad)
        assert response.status_code == 422, response.text

    # 用字符串冒充判断题答案也是 422
    bad = [
        {**item, "answer": "true"}
        if item["question_id"] == true_false["id"]
        else item
        for item in answers
    ]
    assert submit(bad).status_code == 422

    # 满分提交仍然成功
    ok = submit(answers)
    assert ok.status_code == 201, ok.text
    assert ok.json()["total_score"] == 100.0


# --------------------------------------------------------------------------- #
# 发布 / 重试的空对象请求体
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        b'"null"',
        b"[]",
        b"1",
        b"not-json",
        b"\xff\xfe",
        json.dumps({"extra": 1}).encode(),
    ],
    ids=["字符串", "数组", "数字", "非 JSON", "非法 UTF-8", "多余字段"],
)
def test_publish_and_retry_reject_non_empty_object_bodies(
    client: TestClient, fake_storage, pg_session_factory, make_settings, body: bytes
) -> None:
    """发布与重试只接受省略或 ``{}``；其余一律 422，且不改动状态。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-empty-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    # 先出题成功（DRAFT）：状态检查通过后才会校验请求体
    assert (
        _drive_practice_worker(pg_session_factory, make_settings, practice_model_factory())
        == 1
    )

    headers = {**_auth(teacher), "Content-Type": "application/json"}
    publish = client.post(PUBLISH_URL.format(set_id=set_id), content=body, headers=headers)
    assert publish.status_code == 422, publish.text
    assert _error(publish) == "VALIDATION_ERROR"
    assert client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()[
        "status"
    ] == "DRAFT"

    # 省略请求体 / 空对象都可以发布
    omitted = client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))
    assert omitted.status_code == 200, omitted.text
    empty = client.post(
        PUBLISH_URL.format(set_id=set_id), json={}, headers=_auth(teacher)
    )
    assert empty.status_code == 200  # 已发布幂等

    # 显式 null 与省略不同：必须是 422（httpx 的 json=None 等于不发送请求体，
    # 这里必须用原始字节才能真正发出 JSON 的 null）
    null_body = client.post(
        PUBLISH_URL.format(set_id=set_id), content=b"null", headers=headers
    )
    assert null_body.status_code == 422
    assert _error(null_body) == "VALIDATION_ERROR"


def test_retry_empty_body_variants(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """重试：畸形请求体不改状态，空对象与省略都能重置任务。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-retry-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    job_id = generated.json()["id"]

    # 让任务落到 FAILED（模型返回空题目）
    from tests.integration.test_practice_api import _empty_question_model_factory

    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, _empty_question_model_factory()
        )
        == 1
    )

    for body in (b'"null"', b"[1]", b"not-json", b'{"a":1}', b"\xff"):
        response = client.post(
            RETRY_URL.format(job_id=job_id),
            content=body,
            headers={**_auth(teacher), "Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
        # 请求体错误不得消费掉这次重试机会
        assert (
            client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()["status"]
            == "FAILED"
        )

    retried = client.post(
        RETRY_URL.format(job_id=job_id), json={}, headers=_auth(teacher)
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "PENDING"


# --------------------------------------------------------------------------- #
# 提交响应 = 结果接口 = 数据库
# --------------------------------------------------------------------------- #
def test_submit_response_result_and_database_agree(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """部分得分也能保证三处一致：明细之和严格等于总分，且逐字段可回查。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"valid-consistency-{suffix}@example.com",
    )
    student_email = f"valid-consistency-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    _drive_practice_worker(pg_session_factory, make_settings, practice_model_factory())
    client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))

    questions = client.get(
        SET_URL.format(set_id=set_id), headers=_auth(teacher)
    ).json()["questions"]
    answers = _answers_from_teacher_view(questions)

    def _wrong_answer(question: dict) -> object:
        """按题型构造一个"合法但错误"的答案。"""
        if question["type"] == "SINGLE_CHOICE":
            others = [
                option["id"]
                for option in question["options"]
                if option["id"] != question["correct_answer"]
            ]
            return others[0] if others else question["correct_answer"]
        if question["type"] == "TRUE_FALSE":
            return not question["correct_answer"]
        return "完全不相关的内容"

    # 只答对一部分：让总分落到需要余数分配的情形
    partial = []
    for index, (question, item) in enumerate(zip(questions, answers)):
        if index % 2 == 0:
            partial.append({**item, "answer": _wrong_answer(question)})
        else:
            partial.append(item)

    submitted = client.post(
        ATTEMPTS_URL.format(set_id=set_id),
        json={"answers": partial},
        headers=_auth(student),
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    attempt_id = body["id"]

    fetched = client.get(
        RESULT_URL.format(attempt_id=attempt_id), headers=_auth(student)
    )
    assert fetched.status_code == 200
    assert fetched.json() == body

    with pg_sync_engine.connect() as connection:
        attempt_row = (
            connection.execute(
                text(
                    "SELECT total_score FROM practice_attempts"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
        answer_rows = (
            connection.execute(
                text(
                    "SELECT question_id, question_order, score, is_correct,"
                    " submitted_answer FROM practice_attempt_answers"
                    " WHERE attempt_id = CAST(:id AS uuid)"
                    " ORDER BY question_order"
                ),
                {"id": attempt_id},
            )
            .mappings()
            .all()
        )

    assert float(attempt_row["total_score"]) == body["total_score"]
    assert len(answer_rows) == len(body["answers"]) == len(questions)

    # 明细之和严格等于总分；逐字段与响应一致
    detail_sum = round(sum(float(row["score"]) for row in answer_rows), 2)
    assert detail_sum == body["total_score"]
    for row, item in zip(answer_rows, body["answers"]):
        assert str(row["question_id"]) == item["question_id"]
        assert row["question_order"] == item["question_order"]
        assert float(row["score"]) == item["score"]
        assert row["is_correct"] is item["is_correct"]
        assert row["submitted_answer"] == item["submitted_answer"]
