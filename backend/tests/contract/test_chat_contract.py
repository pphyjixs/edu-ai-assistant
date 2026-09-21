"""课程问答接口的契约测试（不访问数据库）。

只校验 OpenAPI 里对外承诺的形状：路径、状态码、Bearer 安全声明、响应组件
与错误码枚举。前端据此生成 TypeScript 类型（``contracts/generated/api-types.ts``），
因此字段缺失或改名必须在这里被拦住（``docs/api-contract.md`` 第 6 节）。
"""

from __future__ import annotations

import pytest

from app.main import create_app

SESSIONS_PATH = "/api/v1/courses/{course_id}/chat-sessions"
MESSAGES_PATH = "/api/v1/chat-sessions/{session_id}/messages"


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def _component(schema: dict, name: str) -> dict:
    components = schema.get("components", {}).get("schemas", {})
    assert name in components, f"缺少响应组件 {name}"
    return components[name]


def _page_component_name(schema: dict, ref: str) -> str:
    name = ref.rsplit("/", 1)[-1]
    # 泛型分页组件的命名由 FastAPI 生成，两种形态都接受
    for candidate in (name, name.replace("%5B", "_").replace("%5D", "_")):
        if candidate in schema.get("components", {}).get("schemas", {}):
            return candidate
    raise AssertionError(f"分页组件不存在：{ref}")


def test_four_chat_endpoints_are_documented(schema: dict) -> None:
    """契约 6.2–6.5 的四个接口必须出现在 OpenAPI 并声明成功状态码。"""
    assert SESSIONS_PATH in schema["paths"]
    assert MESSAGES_PATH in schema["paths"]

    assert schema["paths"][SESSIONS_PATH]["post"]["responses"].keys() >= {"201"}
    assert schema["paths"][SESSIONS_PATH]["get"]["responses"].keys() >= {"200"}
    assert schema["paths"][MESSAGES_PATH]["get"]["responses"].keys() >= {"200"}
    assert schema["paths"][MESSAGES_PATH]["post"]["responses"].keys() >= {"201"}


def test_chat_endpoints_require_bearer_and_declare_errors(schema: dict) -> None:
    """四个接口都挂 Bearer，并声明契约 6.7 的业务错误状态码。"""
    security_schemes = schema["components"]["securitySchemes"]
    assert "HTTPBearer" in security_schemes

    create_op = schema["paths"][SESSIONS_PATH]["post"]
    assert create_op.get("security")
    assert {"401", "404", "409", "422"} <= create_op["responses"].keys()

    list_op = schema["paths"][SESSIONS_PATH]["get"]
    assert list_op.get("security")
    assert {"401", "404", "422"} <= list_op["responses"].keys()

    messages_op = schema["paths"][MESSAGES_PATH]["get"]
    assert messages_op.get("security")
    assert {"401", "404", "422"} <= messages_op["responses"].keys()

    send_op = schema["paths"][MESSAGES_PATH]["post"]
    assert send_op.get("security")
    assert {"401", "404", "409", "422", "502", "503"} <= send_op["responses"].keys()


def test_create_session_response_is_chat_session(schema: dict) -> None:
    ref = schema["paths"][SESSIONS_PATH]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ref.endswith("/ChatSessionSchema")

    session = _component(schema, "ChatSessionSchema")["properties"]
    assert set(session) == {"id", "course_id", "created_at", "last_message_at"}


def test_session_list_is_page_of_chat_session(schema: dict) -> None:
    ref = schema["paths"][SESSIONS_PATH]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    page_name = _page_component_name(schema, ref)
    page = _component(schema, page_name)
    assert set(page["properties"]) == {"items", "page", "page_size", "total"}


def test_message_schema_exposes_grounded_and_citations(schema: dict) -> None:
    """契约 6.6：助手消息必须有 grounded 与 citations，用户消息 grounded 为 null。"""
    message = _component(schema, "ChatMessageSchema")["properties"]
    assert set(message) == {
        "id",
        "session_id",
        "role",
        "content",
        "grounded",
        "citations",
        "created_at",
    }

    citation = _component(schema, "Citation")["properties"]
    assert set(citation) == {
        "material_id",
        "material_name",
        "section_id",
        "section_title",
        "source_type",
        "location_start",
        "location_end",
        "page",
        "quote",
    }


def test_send_question_request_has_only_content(schema: dict) -> None:
    """契约 6.5：请求体只有 ``content``，未声明字段拒绝。"""
    ref = schema["paths"][MESSAGES_PATH]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ref.endswith("/ChatQuestionRequest")

    request = _component(schema, "ChatQuestionRequest")
    assert set(request["properties"]) == {"content"}
    assert request.get("additionalProperties") is False


def test_error_code_enum_includes_chat_conflict(schema: dict) -> None:
    error_code = _component(schema, "ErrorCode")
    assert "CHAT_CONFLICT" in error_code["enum"]
    assert "SERVICE_UNAVAILABLE" in error_code["enum"]
    assert "AI_JOB_FAILED" in error_code["enum"]


def test_create_session_request_body_is_optional_object(schema: dict) -> None:
    """契约 6.2：创建会话没有请求字段，请求体是**可选对象**而非可空类型。"""
    request_body = schema["paths"][SESSIONS_PATH]["post"]["requestBody"]
    assert request_body.get("required") in (None, False)

    body_schema = request_body["content"]["application/json"]["schema"]
    assert body_schema.get("type") == "object"
    assert "anyOf" not in body_schema, "省略合法，但显式 null 不合法"
    assert body_schema.get("nullable") is not True


def test_send_question_documents_model_failure_statuses(schema: dict) -> None:
    """契约 6.7：需要调用模型时的失败状态码与冲突状态码都要声明。"""
    send_op = schema["paths"][MESSAGES_PATH]["post"]
    assert {"409", "502", "503"} <= send_op["responses"].keys()
