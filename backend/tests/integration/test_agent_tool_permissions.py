"""Agent 工具权限与可见性的集成测试（开发方案 10.2 的越权场景）。

原则：**模型输出不构成授权**。工具执行前，服务端会重新确认角色、
课程成员关系与实体归属；越权一律返回稳定错误码，且不泄露资源名。
"""

from __future__ import annotations

import json
import uuid

from app.modules.agent.orchestration import MAX_WRITE_CALLS
from tests.integration.test_agent_tool_runs import (
    AgentScript,
    _completion,
    _create_assignment,
    _join_course,
    _login,
    _register,
    _rows,
    _run_agent,
    _tool_call,
    create_run,
    create_session,
    get_run,
    list_messages,
    teacher_with_ready_material,
)

# 复用 runs 模块的客户端与内存对象存储夹具（pytest 会把导入的夹具当作本模块夹具）
from tests.integration.test_agent_tool_runs import client as client  # noqa: F401
from tests.integration.test_agent_tool_runs import fake_storage as fake_storage  # noqa: F401


def test_student_cannot_create_practice(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """学生要求生成正式练习：零写入，回答说明权限限制（开发方案 10.2）。"""
    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    _register(client, "agent-perm-student@example.com", "STUDENT")
    student = _login(client, "agent-perm-student@example.com")
    _join_course(client, student, teacher, course_id)

    session_id = create_session(client, student, course_id)
    run = create_run(
        client, student, session_id, input_text="生成 10 道中等难度的单选题"
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
                            "question_count": 10,
                            "question_types": ["SINGLE_CHOICE"],
                            "difficulty": "MEDIUM",
                        },
                    )
                ]
            ),
            _completion(
                {
                    "answer": "创建正式练习目前仅限课程教师，我无法为你创建。",
                    "citations": [],
                }
            ),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, student, run["id"])
    assert final["status"] == "SUCCEEDED"
    assert final["artifacts"] == []
    assert any(step["error_code"] == "FORBIDDEN" for step in final["steps"])

    # 零写入：既没有练习，也没有生成任务
    assert _rows(pg_sync_engine, "SELECT count(*) AS t FROM practice_sets")[0]["t"] == 0
    assert (
        _rows(
            pg_sync_engine,
            "SELECT count(*) AS t FROM jobs WHERE type = 'PRACTICE_GENERATE'",
        )[0]["t"]
        == 0
    )

    assistant = list_messages(client, student, session_id)[-1]
    assert assistant["role"] == "ASSISTANT"
    assert "教师" in assistant["content"]


def test_material_from_another_course_is_not_found(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """跨课程资料：工具返回 RESOURCE_NOT_FOUND，且不泄露资料名（开发方案 10.2）。"""
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    # 另一门课（另一位教师）的已解析资料
    other_course_id, _other_teacher, other_material_id = teacher_with_ready_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="agent-perm-other-teacher@example.com",
    )
    assert other_course_id != course_id

    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="检索这份资料")

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call(
                        "call_search",
                        "search_course_knowledge",
                        {"query": "软件工程", "material_ids": [other_material_id]},
                    )
                ]
            ),
            _completion({"answer": "这份资料不在当前课程中。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["steps"][0]["error_code"] == "RESOURCE_NOT_FOUND"

    payload = script.last_tool_payload()
    assert payload["ok"] is False
    # 不泄露另一门课的资料标识与文件名
    assert other_material_id not in json.dumps(payload)
    assert "chapter-1.docx" not in json.dumps(payload)


def test_assignment_from_another_course_is_not_found(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """跨课程作业：``get_assignment`` 统一 RESOURCE_NOT_FOUND。"""
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    other_course_id, other_teacher, _ = teacher_with_ready_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="agent-perm-other-teacher2@example.com",
    )
    foreign_assignment_id = _create_assignment(
        client, other_teacher, other_course_id, title="别的课程的作业"
    )

    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="看看这份作业要求")

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call(
                        "call_assign",
                        "get_assignment",
                        {"assignment_id": foreign_assignment_id},
                    )
                ]
            ),
            _completion({"answer": "这份作业不在当前课程中。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    final = get_run(client, teacher, run["id"])
    assert final["steps"][0]["error_code"] == "RESOURCE_NOT_FOUND"
    payload = script.last_tool_payload()
    assert foreign_assignment_id not in json.dumps(payload)
    assert "别的课程的作业" not in json.dumps(payload)


def test_write_limit_is_one_per_run(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """同一 Run 最多一次成功的写工具（开发方案 5.6 的生产限制）。"""
    assert MAX_WRITE_CALLS == 1

    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    create_run(
        client, teacher, session_id, input_text="生成一套练习，另外再生成一套练习"
    )

    def _args(count: int) -> dict:
        return {
            "material_ids": [material_id],
            "question_count": count,
            "question_types": ["SINGLE_CHOICE"],
            "difficulty": "MEDIUM",
        }

    script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call("c1", "generate_practice", _args(5)),
                    _tool_call("c2", "generate_practice", _args(8)),
                ]
            ),
            _completion({"answer": "已经创建了一套练习。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, script) == 1

    assert _rows(pg_sync_engine, "SELECT count(*) AS t FROM practice_sets")[0]["t"] == 1
    codes = [
        step["error_code"]
        for step in _rows(
            pg_sync_engine,
            "SELECT error_code FROM agent_run_steps ORDER BY step_order",
        )
    ]
    assert "WRITE_LIMIT_REACHED" in codes
    assert uuid.UUID(material_id)  # 资料 id 是合法的 UUID（参数校验通过）
