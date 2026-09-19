"""健康检查验收测试（真实 PostgreSQL 测试库）。

对应 ``docs/deployment-vercel.md`` 第 5 节与 ``docs/acceptance.md`` 第 10 节：

- ``/health/live``：进程能响应即 200，不访问任何外部服务；
- ``/health/ready``：配置齐备 + 数据库可达才 200，否则 503 且为统一错误结构。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

LIVE_URL = "/health/live"
READY_URL = "/health/ready"

#: 无人监听的端口，用于模拟数据库不可达
UNREACHABLE_DATABASE_URL = "postgresql://tester:secret@127.0.0.1:59999/edu_ai"


def test_liveness_returns_200_and_does_not_cache(api_client) -> None:
    response = api_client.get(LIVE_URL)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "check": "live"}
    assert response.headers["cache-control"] == "no-store"


def test_readiness_ok_with_real_database(api_client) -> None:
    """独立测试库 + 迁移建表后的就绪结果。"""
    response = api_client.get(READY_URL)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"config": "ok", "database": "ok"},
    }
    assert response.headers["cache-control"] == "no-store"


def test_readiness_returns_503_when_required_config_missing(
    db_isolation, pg_app
) -> None:
    """缺必需配置时 503，且 details 指出缺失项名称（不含任何密钥取值）。"""
    with TestClient(pg_app(database_url="")) as client:
        response = client.get(READY_URL)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["details"]["checks"] == {
        "config": "unavailable",
        "database": "unavailable",
    }
    assert any("DATABASE_URL" in item for item in error["details"]["config_problems"])
    assert "secret" not in response.text.lower()


def test_readiness_rejects_weak_secret_outside_development(db_isolation, pg_app) -> None:
    """preview/production 使用占位密钥时必须判定为未就绪。"""
    with TestClient(
        pg_app(app_env="production", app_secret_key="changeme")
    ) as client:
        response = client.get(READY_URL)

    assert response.status_code == 503
    problems = response.json()["error"]["details"]["config_problems"]
    assert any("APP_SECRET_KEY" in item for item in problems)


def test_readiness_returns_503_when_database_unreachable(db_isolation, pg_app) -> None:
    """数据库不可达时 503，问题说明已脱敏（不含用户名与密码）。"""
    with TestClient(
        pg_app(
            database_url=UNREACHABLE_DATABASE_URL,
            db_ready_timeout_seconds=2.0,
        )
    ) as client:
        response = client.get(READY_URL)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["details"]["checks"] == {"config": "ok", "database": "unavailable"}
    assert "database_problem" in error["details"]
    assert "secret" not in response.text.lower()
    assert "tester" not in response.text


def test_health_endpoints_are_reachable_without_authentication(api_client) -> None:
    """探针不带任何凭据即可访问，且不在 /api/v1 之下。"""
    assert api_client.get(LIVE_URL).status_code == 200
    assert api_client.get(READY_URL).status_code == 200
    assert api_client.get("/api/v1/health/live").status_code == 404
