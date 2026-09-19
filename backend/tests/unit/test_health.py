"""健康检查测试。

覆盖 ``docs/deployment-vercel.md`` 第 5 节与 ``docs/acceptance.md`` 第 10 节：
``/health/live`` 不访问外部服务；``/health/ready`` 在配置缺失或数据库不可达时
返回 503。
"""

from __future__ import annotations

import pytest

from app.core.request_context import REQUEST_ID_HEADER
from app.db.session import DatabaseUnavailableError

READY_CHECKS_OK = {"config": "ok", "database": "ok"}


@pytest.fixture
def database_probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """把数据库探测替换为 fake，记录调用次数，避免连接真实数据库。"""
    calls: list[object] = []

    async def _ping(settings: object, *, timeout_seconds: float | None = None) -> None:
        calls.append(settings)

    monkeypatch.setattr("app.api.health.ping_database", _ping)
    return calls


def test_liveness_does_not_touch_dependencies(
    make_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存活检查不得访问数据库。"""

    async def _unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("/health/live 不应访问数据库")

    monkeypatch.setattr("app.api.health.ping_database", _unexpected)

    response = make_client().get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "check": "live"}
    assert response.headers["cache-control"] == "no-store"


def test_readiness_ok_when_config_and_database_ready(
    make_client, database_probe_calls: list[object]
) -> None:
    response = make_client().get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": READY_CHECKS_OK}
    assert len(database_probe_calls) == 1, "就绪检查应实际探测数据库"


def test_readiness_reports_missing_required_config(
    make_client, database_probe_calls: list[object]
) -> None:
    """DATABASE_URL 缺失时必须 503，并指出缺失项名称。"""
    response = make_client(database_url="").get("/health/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["request_id"]
    assert error["details"]["checks"] == {"config": "unavailable", "database": "unavailable"}
    assert any("DATABASE_URL" in item for item in error["details"]["config_problems"])
    assert database_probe_calls == [], "连接串缺失时不应尝试连接"


def test_readiness_reports_database_failure(
    make_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库不可达时返回 503，且问题说明已脱敏可安全展示。"""

    async def _fail(settings: object, *, timeout_seconds: float | None = None) -> None:
        raise DatabaseUnavailableError("数据库连接超时（超过 3 秒）")

    monkeypatch.setattr("app.api.health.ping_database", _fail)

    response = make_client().get("/health/ready")

    assert response.status_code == 503
    details = response.json()["error"]["details"]
    assert details["checks"] == {"config": "ok", "database": "unavailable"}
    assert "超时" in details["database_problem"]


def test_readiness_rejects_weak_secret_outside_development(
    make_client, database_probe_calls: list[object]
) -> None:
    """preview/production 环境使用占位密钥时必须判定为未就绪。"""
    response = make_client(app_env="production", app_secret_key="changeme").get(
        "/health/ready"
    )

    assert response.status_code == 503
    problems = response.json()["error"]["details"]["config_problems"]
    assert any("APP_SECRET_KEY" in item for item in problems)


def test_request_id_is_propagated_when_provided(make_client, database_probe_calls) -> None:
    response = make_client().get(
        "/health/live", headers={REQUEST_ID_HEADER: "trace-abc-123"}
    )

    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_request_id_is_generated_and_unsafe_value_rejected(
    make_client, database_probe_calls
) -> None:
    client = make_client()

    generated = client.get("/health/live").headers[REQUEST_ID_HEADER]
    assert len(generated) == 32, "缺失时生成 UUID4 十六进制串"

    unsafe = client.get(
        "/health/live", headers={REQUEST_ID_HEADER: "bad id with spaces"}
    ).headers[REQUEST_ID_HEADER]
    assert unsafe != "bad id with spaces"
