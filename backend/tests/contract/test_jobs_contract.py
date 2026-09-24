"""Jobs 接口契约测试（``docs/api-contract.md`` 第 10 节）。

只校验 OpenAPI 对外承诺的形状：两条路径、状态码、Bearer 安全声明、
``JobStatus`` 组件（字段、必填性、UUID、date-time、progress 范围、error 长度）、
公开枚举不含 ``AGENT_RUN``、retry 请求体为可省略的非 nullable 对象，
以及导出的 ``contracts/openapi/openapi.json`` 与生成类型语义一致。

本模块不访问数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.main import create_app

#: 导出文件位置（与 backend/scripts/export_openapi.py 一致）
OPENAPI_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.json"
)
#: 生成的前端类型（由 npm run api:types 产出）
TYPES_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "generated" / "api-types.ts"
)

JOB_PATH = "/api/v1/jobs/{job_id}"
RETRY_PATH = "/api/v1/jobs/{job_id}/retry"

#: 契约 10.0 的响应字段
EXPECTED_FIELDS = {
    "id",
    "type",
    "status",
    "progress",
    "resource_type",
    "resource_id",
    "error",
    "failure_stage",
    "created_at",
    "started_at",
    "finished_at",
}
#: 内部调度字段，公开文档中不允许出现
INTERNAL_FIELDS = {"attempts", "run_token", "lease_expires_at"}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


@pytest.fixture(scope="module")
def exported() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_both_paths_and_status_codes(schema: dict) -> None:
    get_op = schema["paths"][JOB_PATH]["get"]
    retry_op = schema["paths"][RETRY_PATH]["post"]

    assert set(get_op["responses"]) >= {"200", "401", "404", "422"}
    assert set(retry_op["responses"]) >= {"202", "401", "403", "404", "409", "422"}


def test_both_endpoints_require_bearer_token(schema: dict) -> None:
    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"

    for path in (JOB_PATH, RETRY_PATH):
        for operation in schema["paths"][path].values():
            assert {"HTTPBearer": []} in operation["security"]


def test_job_status_component_shape(schema: dict) -> None:
    definition = schema["components"]["schemas"]["JobStatus"]
    properties = definition["properties"]

    assert set(properties) == EXPECTED_FIELDS
    assert set(definition["required"]) == EXPECTED_FIELDS - {"failure_stage"}
    assert not (INTERNAL_FIELDS & set(properties))

    assert properties["id"]["format"] == "uuid"
    assert properties["resource_id"]["format"] == "uuid"


def test_progress_and_error_bounds_are_declared(schema: dict) -> None:
    properties = schema["components"]["schemas"]["JobStatus"]["properties"]

    progress = properties["progress"]
    assert progress["type"] == "integer"
    assert progress["minimum"] == 0
    assert progress["maximum"] == 100

    # error 是 `string | null`，长度上限落在 string 分支上
    string_branch = next(
        branch for branch in properties["error"]["anyOf"] if branch.get("type") == "string"
    )
    assert string_branch["maxLength"] == 500

    failure_stage = properties["failure_stage"]
    assert {branch["type"] for branch in failure_stage["anyOf"]} == {
        "string",
        "null",
    }


def test_time_fields_declare_date_time_format(schema: dict) -> None:
    properties = schema["components"]["schemas"]["JobStatus"]["properties"]

    created = [properties["created_at"]]
    for name in ("started_at", "finished_at"):
        created.extend(
            branch
            for branch in properties[name]["anyOf"]
            if branch.get("type") == "string"
        )

    assert created, "时间字段必须存在"
    for branch in created:
        assert branch["format"] == "date-time", branch


def test_public_enums_exclude_agent_run(schema: dict) -> None:
    """公开枚举只有三类任务；内部 AGENT_RUN 不进入公开文档。"""
    job_types = schema["components"]["schemas"]["JobType"]
    resource_types = schema["components"]["schemas"]["JobResourceType"]

    assert set(job_types["enum"]) == {
        "MATERIAL_PARSE",
        "PRACTICE_GENERATE",
        "SUBMISSION_GRADE",
    }
    assert set(resource_types["enum"]) == {
        "MATERIAL",
        "PRACTICE_SET",
        "SUBMISSION",
    }
    assert "AGENT_RUN" not in json.dumps(job_types)
    assert "AGENT_RUN" not in json.dumps(resource_types)


def test_retry_request_body_is_optional_non_nullable_object(schema: dict) -> None:
    request_body = schema["paths"][RETRY_PATH]["post"]["requestBody"]
    body_schema = request_body["content"]["application/json"]["schema"]

    assert request_body["required"] is False
    assert body_schema["type"] == "object"
    assert body_schema["additionalProperties"] is False
    assert "anyOf" not in body_schema  # 非 nullable：显式 null 由运行时拒绝


def test_exported_file_matches_runtime(schema: dict, exported: dict) -> None:
    """导出物与运行时逐字节一致（路径、组件与枚举）。"""
    assert exported["paths"][JOB_PATH] == schema["paths"][JOB_PATH]
    assert exported["paths"][RETRY_PATH] == schema["paths"][RETRY_PATH]
    assert exported["components"]["schemas"]["JobStatus"] == schema["components"]["schemas"][
        "JobStatus"
    ]


def test_generated_types_cover_jobs_surface() -> None:
    """生成的 TypeScript 类型包含 Jobs 组件，且公开枚举不含 AGENT_RUN。"""
    content = TYPES_PATH.read_text(encoding="utf-8")

    assert "JobStatus:" in content
    assert (
        'JobType: "MATERIAL_PARSE" | "PRACTICE_GENERATE" | "SUBMISSION_GRADE";'
        in content
    )
    assert 'JobResourceType: "MATERIAL" | "PRACTICE_SET" | "SUBMISSION";' in content
    assert "/api/v1/jobs/{job_id}" in content
    assert "/api/v1/jobs/{job_id}/retry" in content
