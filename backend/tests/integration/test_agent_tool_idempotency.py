"""Agent 写工具幂等的集成测试（开发方案 10.2 的重试场景）。

核心场景：Worker 在**写工具已完成**之后丢失租约/失败重试。

如果幂等只按模型的 ``tool_call_id`` 去重，重试时模型会生成一批新的 call id，
同一个"生成练习"请求会被执行两次，产生两套练习。因此写工具用
``(run_id, 工具名, 规范化参数)`` 的摘要作为步骤键，重试直接重放已有结果。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.integration.test_agent_tool_runs import (
    AgentScript,
    _completion,
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


def _reset_job_for_retry(pg_sync_engine, run_id: str) -> None:
    """把任务退回 PENDING，模拟"执行者丢失租约、任务被重新领取"。"""
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = 'PENDING', run_token = NULL,"
                " lease_expires_at = NULL, attempts = 0, error = NULL,"
                " failure_stage = NULL, started_at = NULL, finished_at = NULL"
                " WHERE type = 'AGENT_RUN' AND resource_id = :run_id"
            ),
            {"run_id": run_id},
        )


@pytest.mark.parametrize("lost_audit_step", [False, True])
def test_retry_after_write_does_not_duplicate_practice_set(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine, lost_audit_step
) -> None:
    """Worker 在写工具完成后重试：PracticeSet 数量仍为 1（开发方案 10.2 / 11.2）。"""
    course_id, teacher, material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(
        client, teacher, session_id, input_text="根据课件生成 5 道中等难度的单选题"
    )

    arguments = {
        "material_ids": [material_id],
        "question_count": 5,
        "question_types": ["SINGLE_CHOICE"],
        "difficulty": "MEDIUM",
    }

    # 第一次执行：工具执行成功（练习已创建），但最终回答不可解析 → Run FAILED
    first_script = AgentScript(
        [
            _completion(tool_calls=[_tool_call("call_1", "generate_practice", arguments)]),
            _completion("这不是 JSON"),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, first_script) == 1

    failed = get_run(client, teacher, run["id"])
    assert failed["status"] == "FAILED"
    assert _rows(pg_sync_engine, "SELECT count(*) AS t FROM practice_sets")[0]["t"] == 1

    # 第二次执行：模型重新发起**同一个**语义请求（call id 不同，参数相同）
    if lost_audit_step:
        # 模拟领域事务已提交，但 Worker 在记录步骤前中断。
        with pg_sync_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM agent_run_steps WHERE run_id = :run_id"),
                {"run_id": run["id"]},
            )
    _reset_job_for_retry(pg_sync_engine, run["id"])

    second_script = AgentScript(
        [
            _completion(tool_calls=[_tool_call("call_2", "generate_practice", arguments)]),
            _completion({"answer": "已经创建了一套 5 道题的练习。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, second_script) == 1

    # 关键断言：没有第二套练习，也没有第二个生成任务
    assert _rows(pg_sync_engine, "SELECT count(*) AS t FROM practice_sets")[0]["t"] == 1
    assert (
        _rows(
            pg_sync_engine,
            "SELECT count(*) AS t FROM jobs WHERE type = 'PRACTICE_GENERATE'",
        )[0]["t"]
        == 1
    )

    succeeded = get_run(client, teacher, run["id"])
    assert succeeded["status"] == "SUCCEEDED"
    # 重放的结果仍然作为 artifact 呈现给用户
    assert len(succeeded["artifacts"]) == 1
    assert succeeded["artifacts"][0]["kind"] == "PRACTICE_SET"

    # 写入的步骤只有一条（幂等键相同，重放不新增行）
    rows = _rows(
        pg_sync_engine,
        "SELECT name, status, call_id FROM agent_run_steps WHERE run_id = :run_id"
        " ORDER BY step_order",
        run_id=run["id"],
    )
    practice_steps = [row for row in rows if row["name"] == "generate_practice"]
    assert len(practice_steps) == 1
    assert practice_steps[0]["status"] == "SUCCEEDED"
    assert practice_steps[0]["call_id"].startswith("idem:")

    # 助手消息只写了一条
    messages = list_messages(client, teacher, session_id)
    assert [item["role"] for item in messages] == ["USER", "ASSISTANT"]


def test_read_tool_replays_without_re_execution(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """只读工具同样按 ``(run_id, call_id)`` 幂等：重放不重复执行 handler。"""
    course_id, teacher, _material_id = teacher_with_ready_material(
        client, fake_storage, pg_session_factory, make_settings
    )
    session_id = create_session(client, teacher, course_id)
    run = create_run(client, teacher, session_id, input_text="总结这门课")

    first_script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call("call_1", "search_course_knowledge", {"query": "软件工程"})
                ]
            ),
            _completion("坏掉的输出"),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, first_script) == 1
    assert get_run(client, teacher, run["id"])["status"] == "FAILED"

    _reset_job_for_retry(pg_sync_engine, run["id"])

    second_script = AgentScript(
        [
            _completion(
                tool_calls=[
                    _tool_call("call_1", "search_course_knowledge", {"query": "软件工程"})
                ]
            ),
            _completion({"answer": "已经检索完成。", "citations": []}),
        ]
    )
    assert _run_agent(pg_session_factory, make_settings, second_script) == 1

    # 步骤没有新增（同一个 call_id 走重放）
    rows = _rows(
        pg_sync_engine,
        "SELECT count(*) AS t FROM agent_run_steps WHERE run_id = :run_id",
        run_id=run["id"],
    )
    assert rows[0]["t"] == 1
    assert get_run(client, teacher, run["id"])["status"] == "SUCCEEDED"
