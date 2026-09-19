"""CORS 来源限制测试。

对应 ``docs/deployment-vercel.md`` 第 1 节：只允许正式前端域名、本地开发地址
和显式配置的预览域名策略。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://not-our-frontend.example.com"
PREVIEW_ORIGIN = "https://edu-ai-frontend-pr-42.vercel.app"
PREVIEW_ORIGIN_REGEX = r"^https://edu-ai-frontend-[a-z0-9-]+\.vercel\.app$"


def test_listed_origin_is_allowed_with_credentials(make_client) -> None:
    response = make_client().get("/health/live", headers={"Origin": ALLOWED_ORIGIN})

    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unlisted_origin_gets_no_cors_headers(make_client) -> None:
    response = make_client().get("/health/live", headers={"Origin": FOREIGN_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


def test_preflight_respects_origin_allowlist(make_client) -> None:
    client = make_client()
    preflight_headers = {"Access-Control-Request-Method": "GET"}

    allowed = client.options(
        "/health/live", headers={**preflight_headers, "Origin": ALLOWED_ORIGIN}
    )
    rejected = client.options(
        "/health/live", headers={**preflight_headers, "Origin": FOREIGN_ORIGIN}
    )

    assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-origin" not in rejected.headers


def test_preview_regex_policy_allows_preview_deployment(make_settings) -> None:
    app = create_app(make_settings(frontend_origin_regex=PREVIEW_ORIGIN_REGEX))

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"Origin": PREVIEW_ORIGIN})

    assert response.headers["access-control-allow-origin"] == PREVIEW_ORIGIN


def test_wildcard_origin_fails_fast(make_settings) -> None:
    """通配来源与凭据不能共存，必须在启动阶段暴露。"""
    try:
        create_app(make_settings(frontend_origins="*"))
    except ValueError as exc:
        assert "通配符" in str(exc)
    else:  # pragma: no cover - 配置正确时不会进入
        raise AssertionError("FRONTEND_ORIGINS 使用 * 时应当直接失败")
