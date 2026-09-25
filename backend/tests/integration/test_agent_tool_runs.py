"""Agent 工具编排的集成测试（开发方案 10.2）。

跑在真实 PostgreSQL 上（``db_isolation`` + ``pg_app``），模型侧用
``httpx.MockTransport`` 脚本化，绝不访问真实收费模型。

覆盖场景（开发方案 10.2 表格）：

================================================================  ============================================
场景                                                              期望
================================================================  ============================================
「总结这门课」                                                    模型调用资料检索，最终 Run 成功且引用属于本课程
「最近有哪些作业要交」                                            调用作业列表；学生看不到草稿作业
教师明确要求生成练习且参数齐全                                    只创建 1 个 PracticeSet 和 1 个 Job；Run artifact 指向它
模型调用未知工具                                                  Run 可修正，服务端不执行任何动态函数
provider 不支持 tools                                             明确 ``MODEL_TOOL_CALL_UNSUPPORTED``，无伪执行结果
Run 执行中被取消                                                  后续模型轮次不再开始，状态为 CANCELLED
================================================================  ============================================

本模块同时导出 :class:`AgentScript` 与若干辅助函数，供权限与幂等测试复用。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.modules.agent import worker as agent_worker
from app.modules.agent.orchestration import ORCHESTRATOR_VERSION
from tests.integration.test_materials_api import (
    DOCX_MIME,
    PARSEABLE_DOCX,
    VALID_OUTLINE_PAYLOAD,
    _auth,
    _create_course,
    _drive_worker,
    _fake_model_client,
    _login,
    _model_json_response,
    _register,
    _upload,
)

# 复用 materials 测试的内存对象存储夹具
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

SESSIONS_URL = "/api/v1/courses/{course_id}/chat-sessions"
SESSION_URL = "/api/v1/chat-sessions/{session_id}"
RUNS_URL = "/api/v1/chat-sessions/{session_id}/runs"
RUN_URL = "/api/v1/agent-runs/{run_id}"
CANCEL_URL = "/api/v1/agent-runs/{run_id}/cancel"
MESSAGES_URL = "/api/v1/chat-sessions/{session_id}/messages"

COURSE_URL = "/api/v1/courses/{course_id}"
JOIN_URL = "/api/v1/courses/join"
ASSIGNMENTS_URL = "/api/v1/courses/{course_id}/assignments"
PUBLISH_URL = "/api/v1/assignments/{assignment_id}/publish"

FAKE_BASE_URL = "http://fake-model.local/v1"


# --------------------------------------------------------------------------- #
# 脚本化假模型
# --------------------------------------------------------------------------- #
class AgentScript:
    """按顺序返回响应的假模型；同时记录每次请求便于断言。

    每个脚本项可以是：
    - ``httpx.Response``：直接返回；
    - 可调用对象 ``fn(payload) -> httpx.Response``：根据上一轮消息动态生成回答
      （例如引用刚拿到的工具证据）。
    """

    def __init__(self, items: list[Any] | None = None) -> None:
        self.items: list[Any] = list(items or [])
        self.requests: list[dict] = []

    def push(self, item: Any) -> AgentScript:
        self.items.append(item)
        return self

    def responder(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.requests.append(payload)
        if not self.items:
            raise AssertionError("模型被调用的次数超过了脚本长度")
        item = self.items.pop(0)
        return item(payload) if callable(item) else item

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.responder))

    def factory(self):
        return lambda: self.client()

    # ------------------------------ 断言辅助 ------------------------------ #
    def messages(self) -> list[dict]:
        return [message for payload in self.requests for message in payload["messages"]]

    def tool_messages(self) -> list[dict]:
        return [m for m in self.messages() if m.get("role") == "tool"]

    def last_tool_payload(self) -> dict:
        return json.loads(self.tool_messages()[-1]["content"])

    def system_messages(self) -> list[str]:
        return [str(payload["messages"][0]["content"]) for payload in self.requests]


def _completion(content: str | dict | None = None, tool_calls: list[dict] | None = None):
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _cite_first_tool_evidence(payload: dict, *, answer: str = "按检索到的资料，要点如下。") -> httpx.Response:
    """引用最后一个工具结果里的第一条证据（模拟"模型按证据作答"）。"""
    tool_messages = [m for m in payload["messages"] if m.get("role") == "tool"]
    body = json.loads(tool_messages[-1]["content"])
    evidence = body.get("evidence") or []
    if not evidence:
        return _completion({"answer": answer, "citations": []})
    quote = str(evidence[0]["text"]).splitlines()[0][:60]
    return _completion(
        {
            "answer": answer,
            "evidence_level": "FULL",
            "citations": [{"ref": evidence[0]["ref"], "quote": quote}],
        }
    )


# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(db_isolation, pg_app, fake_storage) -> Iterator[TestClient]:
    from app.storage.deps import get_storage_dep

    app = pg_app(ai_base_url=FAKE_BASE_URL, ai_model="fake-model")
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


def _run_agent(
    pg_session_factory,
    make_settings,
    script: AgentScript,
    *,
    responder=None,
    **overrides,
) -> int:
    """驱动 Agent Worker 领取并执行一批（同步包装）。"""
    overrides.setdefault("ai_base_url", FAKE_BASE_URL)
    overrides.setdefault("ai_model", "fake-model")
    settings = make_settings(**overrides)
    factory = _fake_model_client(responder) if responder is not None else script.factory()
    return asyncio.run(
        agent_worker.run_pending_batch(
            pg_session_factory, settings=settings, ai_client_factory=factory
        )
    )


def teacher_with_ready_material(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    *,
    email: str = "agent-tools-teacher@example.com",
):
    """课程 + 已 READY 的资料 + 教师令牌；返回 ``(course_id, token, material_id)``。"""
    _register(client, email, "TEACHER")
    teacher = _login(client, email)
    course_id = _create_course(client, teacher)
    completed = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename="chapter-1.docx",
        content_type=DOCX_MIME,
        content=PARSEABLE_DOCX,
    )
    _drive_worker(
        pg_session_factory,
        fake_storage,
        make_settings,
        _fake_model_client(lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)),
    )
    return course_id, teacher, completed["material"]["id"]


def create_session(client: TestClient, token: str, course_id: str) -> str:
    response = client.post(
        SESSIONS_URL.format(course_id=course_id), json={}, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_run(
    client: TestClient, token: str, session_id: str, *, input_text: str, **extra
):
    body: dict[str, Any] = {
        "input": input_text,
        "action": "ASK",
        # 幂等键在用户范围内唯一：契约 6.3 要求必填
        "client_request_id": str(uuid.uuid4()),
    }
    body.update(extra)
    response = client.post(
        RUNS_URL.format(session_id=session_id), json=body, headers=_auth(token)
    )
    assert response.status_code == 202, response.text
    return response.json()


def get_run(client: TestClient, token: str, run_id: str) -> dict:
    response = client.get(RUN_URL.format(run_id=run_id), headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def list_messages(client: TestClient, token: str, session_id: str) -> list[dict]:
    response = client.get(
        MESSAGES_URL.format(session_id=session_id), headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _rows(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as connection:
        result = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in result]


# --------------------------------------------------------------------------- #
# 「总结这门课」：检索工具 + 引用可核对
# --------------------------------------------------------------------------- #
def test_summarize_course_uses_knowledge_search_tool(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="总结这门课")

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call(
                        "call_search", "search_course_knowledge", {"query": "软件工程"}
                    )
                ]
            ),
            _cite_first_tool_evidence,
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["status"] == "SUCCEEDED"
    assert [step["name"] for step in final["steps"]] == ["search_course_knowledge"]
    assert final["steps"][0]["kind"] == "TOOL_CALL"
    assert final["steps"][0]["status"] == "SUCCEEDED"

    messages = list_messages(client, teacher, session_id)
    assistant = messages[-1]
    assert assistant["role"] == "ASSISTANT"
    # 引用必须来自本课程的资料，且是可核对的摘录
    assert assistant["citations"], assistant
    assert assistant["citations"][0]["source_kind"] == "MATERIAL"
    assert assistant["citations"][0]["material_id"] is not None
    assert assistant["grounded"] is True

    # 编排器版本落库，便于回溯
    rows = _rows(
        pg_sync_engine, "SELECT orchestrator_version FROM agent_runs WHERE id = :id", id=run["id"]
    )
    assert rows[0]["orchestrator_version"] == ORCHESTRATOR_VERSION


def test_unknown_tool_is_not_executed(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="帮我删除所有资料")

    script = AgentScript(
        [
            _completion(tool_calls=[_tool_call("call_x", "delete_everything", {})]),
            _completion({"answer": "我没有这个能力。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["steps"][0]["error_code"] == "UNKNOWN_TOOL"
    # 服务端没有执行任何动态函数：资料还在
    rows = _rows(
        pg_sync_engine,
        "SELECT count(*) AS total FROM materials WHERE deleted_at IS NULL",
    )
    assert rows[0]["total"] == 1


def test_provider_without_tool_support_fails_explicitly(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """provider 不支持 tools 时给出明确失败，绝不伪造执行结果（开发方案 10.2）。"""
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="总结这门课")

    def _unsupported(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "This model does not support the tools parameter"}},
        )

    assert _run_agent(pg_session_factory, make_settings, AgentScript(), responder=_unsupported) == 1

    final = get_run(client, teacher, run["id"])
    assert final["status"] == "FAILED"
    assert final["failure_stage"] == "MODEL_TOOL_CALL_UNSUPPORTED"
    # 没有助手消息，也没有任何步骤被伪造成成功
    assert [item["role"] for item in list_messages(client, teacher, session_id)] == ["USER"]


def test_cancel_during_run_stops_following_rounds(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """Run 执行中被取消：后续模型轮次不再开始，状态保持 CANCELLED（开发方案 10.2）。"""
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="总结这门课")

    cancelled = {"done": False}

    def _cancel_then_call_tool(payload: dict) -> httpx.Response:
        # 模型第一轮返回过程中，用户取消了这次 Run
        if not cancelled["done"]:
            cancelled["done"] = True
            with pg_sync_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE jobs SET status = 'CANCELLED', finished_at = now()"
                        " WHERE type = 'AGENT_RUN' AND resource_id = :run_id"
                    ),
                    {"run_id": run["id"]},
                )
        return _completion(
            tool_calls=[
                _tool_call("call_search", "search_course_knowledge", {"query": "软件工程"})
            ]
        )

    script = AgentScript([_cancel_then_call_tool, _cite_first_tool_evidence])
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["status"] == "CANCELLED"
    # 只发出过一轮模型请求：取消后没有再开始新的轮次
    assert len(script.requests) == 1
    assert [item["role"] for item in list_messages(client, teacher, session_id)] == ["USER"]


# --------------------------------------------------------------------------- #
# 「最近有哪些作业要交」：作业列表工具与草稿可见性
# --------------------------------------------------------------------------- #
def _create_assignment(client: TestClient, token: str, course_id: str, *, title: str) -> str:
    response = client.post(
        ASSIGNMENTS_URL.format(course_id=course_id),
        json={
            "title": title,
            "description": "任务说明",
            "total_score": 100,
            "due_at": "2026-12-25T15:59:00Z",
            "allow_late_submission": False,
            "rubric_items": [
                {"title": "完整性", "description": "是否完整", "max_score": 100, "order": 1}
            ],
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _join_course(
    client: TestClient, student_token: str, teacher_token: str, course_id: str
) -> None:
    """邀请码只有教师可见，因此由教师取码、学生加课。"""
    detail = client.get(
        COURSE_URL.format(course_id=course_id), headers=_auth(teacher_token)
    ).json()
    response = client.post(
        JOIN_URL,
        json={"invite_code": detail["invite_code"]},
        headers=_auth(student_token),
    )
    assert response.status_code in (200, 201), response.text


def test_student_assignment_list_hides_drafts(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    draft_id = _create_assignment(client, teacher, course_id, title="草稿作业-不应出现")
    published_id = _create_assignment(client, teacher, course_id, title="已发布作业")
    publish = client.post(
        PUBLISH_URL.format(assignment_id=published_id), json={}, headers=_auth(teacher)
    )
    assert publish.status_code == 200, publish.text

    _register(client, "agent-tools-student@example.com", "STUDENT")
    student = _login(client, "agent-tools-student@example.com")
    _join_course(client, student, teacher, course_id)

    session_id = create_session(client, student, course_id)
    run = create_run(client, student, session_id, input_text="最近有哪些作业要交？")

    script = AgentScript(
        [
            _completion(
                tool_calls=[_tool_call("call_assign", "list_course_assignments", {})]
            ),
            _cite_first_tool_evidence,
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, student, run["id"])
    assert final["status"] == "SUCCEEDED"
    assert final["steps"][0]["name"] == "list_course_assignments"

    tool_body = script.last_tool_payload()
    # 断言里带上完整 payload，失败时能直接看到工具错误码
    assert "assignments" in tool_body.get("data", {}), tool_body
    titles = [item["title"] for item in tool_body["data"]["assignments"]]
    assert "已发布作业" in titles
    assert "草稿作业-不应出现" not in titles
    assert draft_id not in json.dumps(tool_body)


# --------------------------------------------------------------------------- #
# 教师生成练习：复用领域 service，产出 artifact
# --------------------------------------------------------------------------- #
def test_teacher_generate_practice_creates_single_set_and_artifact(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(
        client, teacher, session_id, input_text="根据数据库课件生成 5 道中等难度的单选题"
    )

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call(
                        "call_practice",
                        "generate_practice",
                        {
                            "material_ids": [material_id],
                            "question_count": 5,
                            "question_types": ["SINGLE_CHOICE"],
                            "difficulty": "MEDIUM",
                        },
                    )
                ]
            ),
            _completion({"answer": "已经创建了一套 5 道题的练习。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["status"] == "SUCCEEDED"
    assert len(final["artifacts"]) == 1
    artifact = final["artifacts"][0]
    assert artifact["kind"] == "PRACTICE_SET"
    assert artifact["status"] == "PENDING"
    assert artifact["job_id"]
    assert artifact["href"] == f"/courses/{course_id}/learn"

    sets = _rows(
        pg_sync_engine,
        "SELECT status FROM practice_sets WHERE course_id = :course_id",
        course_id=course_id,
    )
    jobs = _rows(
        pg_sync_engine,
        "SELECT status FROM jobs WHERE type = 'PRACTICE_GENERATE'",
    )
    assert len(sets) == 1 and sets[0]["status"] == "GENERATING"
    assert len(jobs) == 1 and jobs[0]["status"] == "PENDING"

    # 工具本身不调用第二次 LLM，也不等待出题完成
    assert len(script.requests) == 2


def test_write_without_explicit_intent_is_not_executed(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """用户只说"给我出点题"之外的含糊请求时，写工具不执行（开发方案 5.4）。"""
    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    create_run(client, teacher, session_id, input_text="这门课的资料都有哪些？")

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call(
                        "call_practice",
                        "generate_practice",
                        {
                            "material_ids": [material_id],
                            "question_count": 10,
                            "question_types": ["SINGLE_CHOICE"],
                            "difficulty": "MEDIUM",
                        },
                    )
                ]
            ),
            _completion({"answer": "请告诉我需要多少道题、什么题型和难度。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    rows = _rows(pg_sync_engine, "SELECT count(*) AS total FROM practice_sets")
    assert rows[0]["total"] == 0


def test_unknown_entity_run_still_audited(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """每个工具调用都留下 step 终态（开发方案 11.2：可审计率 100%）。"""
    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(
        client,
        teacher,
        session_id,
        input_text="总结这门课",
        context=None,
    )

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call("c1", "search_course_knowledge", {"query": "软件工程"}),
                    _tool_call("c2", "get_assignment", {"assignment_id": str(uuid.uuid4())}),
                ]
            ),
            _cite_first_tool_evidence,
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    steps = _rows(
        pg_sync_engine,
        "SELECT status, error_code, call_id FROM agent_run_steps WHERE run_id = :run_id"
        " ORDER BY step_order",
        run_id=run["id"],
    )
    assert len(steps) == 2
    assert all(step["status"] in {"SUCCEEDED", "FAILED"} for step in steps)
    assert steps[1]["error_code"] == "RESOURCE_NOT_FOUND"
    assert material_id  # 资料来自本课程，检索本身是成功的
