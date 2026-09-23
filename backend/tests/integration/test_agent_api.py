"""Agent Run 的集成测试（``docs/local-development-agent-backend.md`` 第 8 节）。

覆盖点：

- 创建 Run 是**立即返回**的（202 + PENDING），模型调用不发生在请求进程内；
- 幂等键：同一用户重复提交返回同一个 Run 与同一条用户消息；
- 每个会话同时只有一个未结束的 Run（409 ``AGENT_RUN_IN_PROGRESS``）；
- 权限与可见性：非会话所有者、非课程成员、学生不可见的作业、未解析完成的资料；
- 请求体严格校验：未知字段、显式 null、空白 input、超长 input；
- 上下文隔离：MATERIAL 上下文只注入该资料的片段；
- Worker：PENDING → RUNNING → SUCCEEDED，写回助手消息、引用与来源快照；
- 引用校验：越界 ref 与伪造摘录一律丢弃，退化为「无依据」；
- 取消与令牌：取消后不再回写；运行令牌不匹配的旧 Worker 不能覆盖新结果。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.modules.agent import worker as agent_worker
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

# 复用 materials 测试的内存对象存储夹具（pytest 会把导入的夹具当作本模块夹具）
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

SESSIONS_URL = "/api/v1/courses/{course_id}/chat-sessions"
RUNS_URL = "/api/v1/chat-sessions/{session_id}/runs"
RUN_URL = "/api/v1/agent-runs/{run_id}"
CANCEL_URL = "/api/v1/agent-runs/{run_id}/cancel"
MESSAGES_URL = "/api/v1/chat-sessions/{session_id}/messages"

#: 从提示词里取出第一个来源块：``[S1] 标签\n内容：\n<原文>``
_REF_PATTERN = re.compile(
    r"\[(S\d+)\][^\n]*\n内容：\n(.*?)(?=\n\n\[S|\n\n##|\Z)", re.DOTALL
)


def _chat_client(pg_app, fake_storage, ai_client_factory=None) -> TestClient:
    """构造应用；模型客户端通过依赖覆盖注入（不访问真实模型）。"""
    from app.storage.deps import get_storage_dep

    app = pg_app(ai_base_url="http://fake-model.local/v1", ai_model="fake-model")
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    return TestClient(app)


@pytest.fixture
def client(db_isolation, pg_app, fake_storage) -> Iterator[TestClient]:
    """默认客户端：每个用例前后清空业务表（db_isolation）。"""
    with _chat_client(pg_app, fake_storage) as test_client:
        yield test_client


def _quoting_responder(captured: list[str] | None = None, *, ref_override: str | None = None):
    """假模型：引用提示词里第一个来源块的首行原文。"""

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        prompt = payload["messages"][-1]["content"]
        if captured is not None:
            captured.append(prompt)
        match = _REF_PATTERN.search(prompt)
        if match is None:
            return _model_json_response({"answer": "没有可用来源", "citations": []})
        ref = ref_override or match.group(1)
        quote = next(
            (line.strip() for line in match.group(2).splitlines() if line.strip()), ""
        )
        return _model_json_response(
            {"answer": "按注入的资料，要点见引用。", "citations": [{"ref": ref, "quote": quote[:200]}]}
        )

    return responder


def _run_agent_worker(pg_session_factory, make_settings, *, responder=None, **overrides) -> int:
    """驱动 Agent Worker 领取并执行一批（同步包装）。"""
    overrides.setdefault("ai_base_url", "http://fake-model.local/v1")
    overrides.setdefault("ai_model", "fake-model")
    settings = make_settings(**overrides)
    factory = None
    if responder is not None:
        factory = _fake_model_client(responder)
    return asyncio.run(
        agent_worker.run_pending_batch(
            pg_session_factory, settings=settings, ai_client_factory=factory
        )
    )


def _teacher_with_ready_material(
    client, fake_storage, pg_session_factory, make_settings, *, email="teacher@example.com"
):
    """课程 + 已 READY 的资料 + 教师令牌。"""
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


def _create_session(client: TestClient, token: str, course_id: str) -> str:
    response = client.post(
        SESSIONS_URL.format(course_id=course_id), json={}, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_run(client: TestClient, token: str, session_id: str, **body) -> httpx.Response:
    payload = {
        "input": body.pop("input", "这份资料讲了什么？"),
        "action": body.pop("action", "ASK"),
        "client_request_id": body.pop("client_request_id", str(uuid.uuid4())),
        **body,
    }
    return client.post(
        RUNS_URL.format(session_id=session_id), json=payload, headers=_auth(token)
    )


# --------------------------------------------------------------------------- #
# 创建 Run：异步语义、幂等、并发保护
# --------------------------------------------------------------------------- #
def test_create_run_returns_pending_without_calling_model(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)

    response = _create_run(
        client,
        teacher,
        session_id,
        input="总结这份资料",
        action="SUMMARIZE_CONTEXT",
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    # 请求内不调用模型：状态就是 PENDING，进度 0
    assert body["status"] == "PENDING"
    assert body["progress"] == 0
    assert body["output_message_id"] is None
    assert body["action"] == "SUMMARIZE_CONTEXT"
    assert body["session_id"] == session_id
    # 用户消息此时已经落库（历史里能看到），助手消息要等 Worker
    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher))
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()["items"]] == ["USER"]


def test_client_request_id_is_idempotent(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, _ = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    key = str(uuid.uuid4())

    first = _create_run(client, teacher, session_id, client_request_id=key)
    second = _create_run(client, teacher, session_id, client_request_id=key)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]

    # 幂等重放不会再写一条用户消息
    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher))
    assert len(messages.json()["items"]) == 1


def test_only_one_active_run_per_session(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, _ = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)

    assert _create_run(client, teacher, session_id).status_code == 202
    conflict = _create_run(client, teacher, session_id)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "AGENT_RUN_IN_PROGRESS"


# --------------------------------------------------------------------------- #
# 权限与上下文可见性
# --------------------------------------------------------------------------- #
def test_run_requires_session_ownership(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)

    _register(client, "other@example.com", "TEACHER")
    other = _login(client, "other@example.com")
    # 不是会话所有者：统一 404，不区分"不存在"与"不是你的"
    assert _create_run(client, other, session_id).status_code == 404


def test_unknown_or_unsupported_context(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)

    # SUBMISSION 尚未实现：明确 422，而不是假装支持
    unsupported = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "SUBMISSION", "entity_id": str(uuid.uuid4())},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "AGENT_CONTEXT_UNSUPPORTED"

    # 不存在的资料：404（避免 UUID 探测）
    missing = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": str(uuid.uuid4())},
    )
    assert missing.status_code == 404


def test_material_context_requires_ready(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    # 不驱动解析 Worker，资料停在 PROCESSING
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
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
    session_id = _create_session(client, teacher, course_id)

    response = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": completed["material"]["id"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_CONTEXT_NOT_READY"


def test_request_body_strictness(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, _ = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    url = RUNS_URL.format(session_id=session_id)

    base = {"input": "问题", "action": "ASK", "client_request_id": str(uuid.uuid4())}

    unknown = client.post(url, json={**base, "extra": 1}, headers=_auth(teacher))
    assert unknown.status_code == 422

    explicit_null = client.post(url, json={**base, "input": None}, headers=_auth(teacher))
    assert explicit_null.status_code == 422

    blank = client.post(
        url, json={**base, "input": "   "}, headers=_auth(teacher)
    )
    assert blank.status_code == 422

    too_long = client.post(
        url, json={**base, "input": "字" * 2001}, headers=_auth(teacher)
    )
    assert too_long.status_code == 422

    bad_action = client.post(url, json={**base, "action": "DO_MAGIC"}, headers=_auth(teacher))
    assert bad_action.status_code == 422

    # context 内部允许显式 null（文档 6.3 示例）
    ok = client.post(
        url,
        json={
            **base,
            "context": {
                "entity_type": "COURSE",
                "entity_id": None,
                "section_id": None,
                "selected_text": None,
            },
        },
        headers=_auth(teacher),
    )
    assert ok.status_code == 202, ok.text


# --------------------------------------------------------------------------- #
# Worker：成功回写、上下文隔离、引用校验
# --------------------------------------------------------------------------- #
def test_worker_writes_assistant_message_and_sources(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    created = _create_run(
        client,
        teacher,
        session_id,
        input="总结这份资料",
        action="SUMMARIZE_CONTEXT",
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    ).json()
    prompts: list[str] = []

    processed = _run_agent_worker(
        pg_session_factory, make_settings, responder=_quoting_responder(prompts)
    )
    assert processed == 1
    assert len(prompts) == 1
    # 提示词里带上动作模板与资料标识
    assert "总结" in prompts[0]
    assert "chapter-1.docx" in prompts[0]

    run = client.get(RUN_URL.format(run_id=created["id"]), headers=_auth(teacher)).json()
    assert run["status"] == "SUCCEEDED"
    assert run["progress"] == 100
    assert run["output_message_id"] is not None
    assert run["error"] is None
    # 来源快照记录了本次注入的块
    assert {item["source_type"] for item in run["sources"]} <= {
        "MATERIAL_CHUNK",
        "MATERIAL_OUTLINE",
        "COURSE",
        "ASSIGNMENT",
    }
    assert all(item["label"] for item in run["sources"])

    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)).json()
    roles = [item["role"] for item in messages["items"]]
    assert roles == ["USER", "ASSISTANT"]
    assistant = messages["items"][1]
    assert assistant["grounded"] is True
    assert assistant["citations"], "有依据的回答必须带引用"
    citation = assistant["citations"][0]
    assert citation["material_id"] == material_id
    assert citation["quote"]


def test_fake_citation_is_dropped_but_answer_is_kept_without_grounding(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """越界引用只被丢弃，正文保留但如实标注"引用没核对上"。

    评审文档「一、#1.4 / #1.5」：引用验证失败只剔除对应引用，
    不再把整段有效回答抹成一句"未找到依据"。
    """
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    )

    # 引用一个本次上下文里不存在的编号 S99
    _run_agent_worker(
        pg_session_factory, make_settings, responder=_quoting_responder(ref_override="S99")
    )

    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)).json()
    assistant = messages["items"][1]
    # 没有可核对的引用 → 不声称有依据
    assert assistant["grounded"] is False
    assert assistant["citations"] == []
    # 但模型给出的正文被保留下来，并附上"未核对上"的说明
    assert "按注入的资料" in assistant["content"]
    assert "核对" in assistant["content"]


def test_no_evidence_message_names_searched_materials(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """确实无依据时给的说明要具体：说检索了几份资料，而不是一句通用文案。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    )

    def _declines(request: httpx.Request) -> httpx.Response:
        return _model_json_response(
            {"answer": "", "evidence_level": "NONE", "citations": []}
        )

    _run_agent_worker(pg_session_factory, make_settings, responder=_declines)

    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)).json()
    assistant = messages["items"][1]
    assert assistant["grounded"] is False
    assert "1 份资料" in assistant["content"]
    assert "chapter-1.docx" in assistant["content"]


def test_material_context_does_not_leak_other_materials(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, first_material = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    second = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename="secret-second.docx",
        content_type=DOCX_MIME,
        content=PARSEABLE_DOCX,
    )
    _drive_worker(
        pg_session_factory,
        fake_storage,
        make_settings,
        _fake_model_client(lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)),
    )
    session_id = _create_session(client, teacher, course_id)
    prompts: list[str] = []
    _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": first_material},
    )
    _run_agent_worker(
        pg_session_factory, make_settings, responder=_quoting_responder(prompts)
    )

    assert prompts, "应至少调用一次模型"
    assert "chapter-1.docx" in prompts[0]
    assert "secret-second.docx" not in prompts[0]
    assert second["material"]["id"] != first_material


# --------------------------------------------------------------------------- #
# 取消与运行令牌
# --------------------------------------------------------------------------- #
def test_cancel_prevents_worker_writeback(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    created = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    ).json()

    cancelled = client.post(CANCEL_URL.format(run_id=created["id"]), json={}, headers=_auth(teacher))
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "CANCELLED"

    # 已结束的 Run 不能再取消
    again = client.post(CANCEL_URL.format(run_id=created["id"]), json={}, headers=_auth(teacher))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "AGENT_RUN_NOT_CANCELLABLE"

    # Worker 不会认领已取消的任务，也不会写出助手消息
    assert _run_agent_worker(pg_session_factory, make_settings, responder=_quoting_responder()) == 0
    messages = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)).json()
    assert [item["role"] for item in messages["items"]] == ["USER"]


def test_stale_run_token_cannot_overwrite(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """领取后把令牌换掉（模拟"租约过期后新一轮执行"），旧回写必须被拒绝。"""
    from sqlalchemy import update

    from app.core.time import utc_now
    from app.modules.agent import context as context_module
    from app.modules.agent.generation_ai import ValidatedAgentAnswer
    from app.modules.jobs.models import Job, JobStatusValue, JobType

    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    )

    settings = make_settings(ai_base_url="http://fake-model.local/v1", ai_model="fake-model")

    async def _claim_then_rotate():
        claimed = await agent_worker.claim_next(
            pg_session_factory,
            now=utc_now(),
            lease_seconds=settings.agent_run_lease_seconds,
            max_attempts=settings.agent_max_attempts,
        )
        assert claimed is not None
        # 旧 Worker 的令牌被新一轮执行替换
        async with pg_session_factory() as session:
            await session.execute(
                update(Job)
                .where(Job.id == claimed.job_id, Job.type == JobType.AGENT_RUN)
                .values(run_token="rotated-token", status=JobStatusValue.RUNNING)
            )
            await session.commit()
        return claimed

    claimed = asyncio.run(_claim_then_rotate())

    async def _attempt_writeback() -> bool:
        async with pg_session_factory() as session:
            resolved = await context_module.resolve_context(
                session,
                session_id=claimed.session_id,
                course_id=claimed.course_id,
                entity_type=claimed.entity_type,
                entity_id=claimed.entity_id,
                section_id=None,
                selected_text=None,
                question=claimed.question,
                action=claimed.action,
                input_message_id=claimed.input_message_id,
                is_staff=True,
                settings=settings,
            )
        return await agent_worker._write_success(
            pg_session_factory,
            claimed=claimed,
            context=resolved,
            validated=ValidatedAgentAnswer(content="越权写入", grounded=False, citations=[]),
            settings=settings,
            model="fake-model",
            now=utc_now(),
        )

    assert asyncio.run(_attempt_writeback()) is False
    messages = client.get(
        MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)
    ).json()
    assert [item["role"] for item in messages["items"]] == ["USER"]

# --------------------------------------------------------------------------- #
# 评审文档「一、#8 / #9 / #11 / #12 / #13」与「二、5」的回归
# --------------------------------------------------------------------------- #
def _failing_responder(request: httpx.Request) -> httpx.Response:
    """假模型返回不可解析的内容，触发「模型侧失败」路径。"""
    return httpx.Response(200, json={"choices": [{"message": {"content": "不是 JSON"}}]})


def test_action_context_matrix_rejects_meaningless_combinations(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """`CHECK_SUBMISSION + COURSE`、`BREAK_DOWN_ASSIGNMENT + MATERIAL` 必须在创建时就 422。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)

    response = _create_run(
        client,
        teacher,
        session_id,
        action="BREAK_DOWN_ASSIGNMENT",
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "AGENT_CONTEXT_UNSUPPORTED"
    # 提示里要写清允许的组合，前端才能自助修正
    assert "ASSIGNMENT" in body["message"]

    # 拆解任务不允许退化成课程范围
    response = _create_run(client, teacher, session_id, action="BREAK_DOWN_ASSIGNMENT")
    assert response.status_code == 422, response.text

    # 错误优先级：不可见对象先于 action/context 组合判定，因此这里是 404 而不是 422
    response = _create_run(
        client,
        teacher,
        session_id,
        action="BREAK_DOWN_ASSIGNMENT",
        context={"entity_type": "MATERIAL", "entity_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404, response.text

    # 提交前检查只接受 SUBMISSION，课程范围不被接受
    response = _create_run(
        client, teacher, session_id, action="CHECK_SUBMISSION", context=None
    )
    assert response.status_code == 422, response.text

    # 组合合法但领域模块未实现：稳定返回「未实现」，而不是悄悄退化成课程问答
    response = _create_run(
        client,
        teacher,
        session_id,
        action="CHECK_SUBMISSION",
        context={"entity_type": "SUBMISSION", "entity_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "AGENT_CONTEXT_UNSUPPORTED"

    # ASK + COURSE（省略 context）仍然可用
    response = _create_run(client, teacher, session_id)
    assert response.status_code == 202, response.text


def test_same_client_request_id_with_different_body_conflicts(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """幂等键复用于不同内容必须 409，不能把旧答案当成这一次的回答。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    key = str(uuid.uuid4())
    context = {"entity_type": "MATERIAL", "entity_id": material_id}

    first = _create_run(
        client, teacher, session_id, input="第一个问题", client_request_id=key, context=context
    )
    assert first.status_code == 202, first.text

    # 同样的请求体再来一次：返回同一个 Run（网络重试语义）
    replay = _create_run(
        client, teacher, session_id, input="第一个问题", client_request_id=key, context=context
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]

    # 换了内容却复用同一个编号：409
    changed = _create_run(
        client, teacher, session_id, input="换了一个问题", client_request_id=key, context=context
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "AGENT_IDEMPOTENCY_CONFLICT"


def test_active_run_and_run_list_enable_refresh_recovery(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """刷新后能恢复轮询，并能看到上一次失败的原因（评审文档「一、#11」）。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    created = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    ).json()

    active = client.get(
        f"/api/v1/chat-sessions/{session_id}/active-run", headers=_auth(teacher)
    )
    assert active.status_code == 200
    assert active.json()["run"]["id"] == created["id"]
    assert active.json()["run"]["status"] == "PENDING"

    # Worker 执行失败：模型返回不可解析的内容
    _run_agent_worker(pg_session_factory, make_settings, responder=_failing_responder)

    after = client.get(
        f"/api/v1/chat-sessions/{session_id}/active-run", headers=_auth(teacher)
    )
    assert after.status_code == 200
    assert after.json()["run"] is None  # 没有进行中的 Run 是常规状态

    runs = client.get(
        f"/api/v1/chat-sessions/{session_id}/runs", headers=_auth(teacher)
    ).json()
    assert runs["total"] == 1
    failed = runs["items"][0]
    assert failed["id"] == created["id"]
    assert failed["status"] == "FAILED"
    # 失败原因与阶段码都要能读到，前端才能给出「模型侧失败」这类具体提示
    assert failed["error"]
    assert failed["failure_stage"] == "MODEL_CALL"

    # 别人的会话一律 404（不暴露存在性）
    other_token = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings, email="other@example.com"
    )[1]
    assert (
        client.get(
            f"/api/v1/chat-sessions/{session_id}/runs", headers=_auth(other_token)
        ).status_code
        == 404
    )


def test_cancel_rejects_non_empty_body(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """取消接口复用了统一空对象校验器：带字段的请求体是 422。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    run = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    ).json()

    bad = client.post(
        CANCEL_URL.format(run_id=run["id"]),
        json={"reason": "不想等了"},
        headers=_auth(teacher),
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    ok = client.post(CANCEL_URL.format(run_id=run["id"]), json={}, headers=_auth(teacher))
    assert ok.status_code == 202, ok.text
    assert ok.json()["status"] == "CANCELLED"


def test_expired_running_run_is_reclaimed_and_attempts_are_capped(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """过期 RUNNING 可被接管；尝试次数用尽后进入 FAILED（评审文档「一、#9」）。"""
    from datetime import timedelta

    from app.core.time import utc_now

    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    run = _create_run(
        client,
        teacher,
        session_id,
        context={"entity_type": "MATERIAL", "entity_id": material_id},
    ).json()

    async def _scenario() -> object:
        # 第一次领取：租约 0 秒，立刻过期（模拟执行者崩溃）
        first = await agent_worker.claim_next(
            pg_session_factory, now=utc_now(), lease_seconds=0, max_attempts=3
        )
        assert first is not None and first.attempt == 1
        # 新 Worker 应当能接管这条已经过期的 RUNNING
        second = await agent_worker.claim_next(
            pg_session_factory,
            now=utc_now() + timedelta(seconds=1),
            lease_seconds=0,
            max_attempts=3,
        )
        assert second is not None and second.attempt == 2
        # 把上限降到 2：再次领取应当直接判失败，而不是继续重试
        return await agent_worker.claim_next(
            pg_session_factory,
            now=utc_now() + timedelta(seconds=2),
            lease_seconds=300,
            max_attempts=2,
        )

    assert asyncio.run(_scenario()) is None

    detail = client.get(RUN_URL.format(run_id=run["id"]), headers=_auth(teacher)).json()
    assert detail["status"] == "FAILED"
    assert "重试" in detail["error"]


def test_assignment_context_is_valid_evidence(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """作业与评分标准可以支撑有依据的回答，并显示为 ASSIGNMENT 引用（评审文档「一、#2」）。"""
    from tests.integration.test_assignments_api import _create as _create_assignment

    _register(client, "matrix-teacher@example.com", "TEACHER")
    teacher = _login(client, "matrix-teacher@example.com")
    course_id = _create_course(client, teacher)
    assignment = _create_assignment(client, course_id, teacher).json()

    session_id = _create_session(client, teacher, course_id)
    response = _create_run(
        client,
        teacher,
        session_id,
        input="这个实验要怎么做？",
        action="BREAK_DOWN_ASSIGNMENT",
        context={"entity_type": "ASSIGNMENT", "entity_id": assignment["id"]},
    )
    assert response.status_code == 202, response.text

    # 假模型引用提示词里的第一个来源块（就是作业要求块）
    _run_agent_worker(pg_session_factory, make_settings, responder=_quoting_responder())

    messages = client.get(
        MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)
    ).json()
    assistant = messages["items"][1]
    assert assistant["grounded"] is True, assistant["content"]
    citation = assistant["citations"][0]
    assert citation["source_kind"] == "ASSIGNMENT"
    # 作业引用没有页码，也不指向资料阅读器
    assert citation["material_id"] is None
    assert citation["location_start"] is None
    assert "作业" in citation["source_label"]

    # Run 上带本次的依据等级，审计与前端都能看到
    run_detail = client.get(
        RUN_URL.format(run_id=response.json()["id"]), headers=_auth(teacher)
    ).json()
    assert run_detail["evidence_level"] in {"FULL", "PARTIAL"}


def test_material_section_context_scopes_the_prompt(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """MATERIAL_SECTION 只注入该章节及相邻章节，不是整份资料（评审文档「一、#3」）。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    outline = client.get(
        f"/api/v1/materials/{material_id}/outline", headers=_auth(teacher)
    ).json()
    sections = outline["sections"]
    assert len(sections) >= 2

    session_id = _create_session(client, teacher, course_id)
    captured: list[str] = []
    response = _create_run(
        client,
        teacher,
        session_id,
        input="这一节讲了什么？",
        context={
            "entity_type": "MATERIAL_SECTION",
            "entity_id": material_id,
            "section_id": sections[1]["id"],
        },
    )
    assert response.status_code == 202, response.text
    _run_agent_worker(
        pg_session_factory, make_settings, responder=_quoting_responder(captured)
    )

    prompt = captured[0]
    assert sections[1]["title"] in prompt  # 目标章节
    assert sections[0]["title"] in prompt  # 相邻章节作为背景

    # 不存在的 section_id 属于不可见，统一 404
    bad = _create_run(
        client,
        teacher,
        session_id,
        context={
            "entity_type": "MATERIAL_SECTION",
            "entity_id": material_id,
            "section_id": str(uuid.uuid4()),
        },
    )
    assert bad.status_code == 404, bad.text


def test_current_input_appears_only_once_in_prompt(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """本次输入只在「本次输入」区段出现一次，不重复进历史（评审文档「一、#13」）。"""
    course_id, teacher, material_id = _teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = _create_session(client, teacher, course_id)
    context = {"entity_type": "MATERIAL", "entity_id": material_id}

    _create_run(client, teacher, session_id, input="第一轮问题", context=context)
    _run_agent_worker(pg_session_factory, make_settings, responder=_quoting_responder())

    captured: list[str] = []
    _create_run(client, teacher, session_id, input="第二轮问题", context=context)
    _run_agent_worker(
        pg_session_factory, make_settings, responder=_quoting_responder(captured)
    )

    prompt = captured[0]
    assert prompt.count("第二轮问题") == 1
    assert prompt.count("第一轮问题") == 1  # 上一轮作为历史保留一次
    assert "## 本次输入" in prompt
