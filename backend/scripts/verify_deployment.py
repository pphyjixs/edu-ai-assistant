"""对已部署环境执行交付验证清单（Preview / Production / 本地）。

逐项检查 ``docs/deployment-vercel.md`` 第 8.3 节的 Preview 验证项，并打印
可读的通过/失败表，任一失败返回非零码，便于接进 CI 或发布流程。

用法::

    # 本地起服务后再跑
    ..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8123
    ..\\.venv\\Scripts\\python.exe scripts\\verify_deployment.py http://127.0.0.1:8123

    # 直接验证 Preview / Production
    ..\\.venv\\Scripts\\python.exe scripts\\verify_deployment.py https://<preview-domain>

注意：脚本会 **真实注册一个账号**（邮箱按时间戳生成，可重复执行），请只在
Preview 或本地环境运行，不要指向 Production 正式数据。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

import httpx

#: 每个请求的超时；Serverless 冷启动会慢一些，这里给足
REQUEST_TIMEOUT_SECONDS = 30.0

#: 判定 CORS 是否生效时使用的"外部"来源
FOREIGN_ORIGIN = "https://not-our-frontend.example.com"

PASSWORD = "Verify pass 2026!"


@dataclass
class Report:
    """收集检查结果。"""

    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append((name, passed, detail))
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
        return passed

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.results if not item[1]]


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except Exception:  # noqa: BLE001 - 非统一结构时返回空串
        return ""


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except Exception:  # noqa: BLE001
        return ""


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "用法: python scripts/verify_deployment.py <base_url> [allowed_origin]\n"
            "  allowed_origin: 可选，FRONTEND_ORIGINS 里的一个来源，用于验证 CORS 放行"
        )
        return 2

    base_url = sys.argv[1].rstrip("/")
    allowed_origin = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else ""
    report = Report()
    email = f"verify-{int(time.time())}@example.com"
    print(f"目标环境: {base_url}\n测试账号: {email}\n")

    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        # ---------------- 应用入口 / 健康检查 ----------------
        live = client.get("/health/live")
        report.check(
            "GET /health/live 返回 200 且不依赖外部服务",
            live.status_code == 200 and live.json().get("check") == "live",
            f"status={live.status_code} body={live.text[:80]}",
        )
        report.check(
            "响应带 X-Request-ID",
            bool(live.headers.get("x-request-id")),
            live.headers.get("x-request-id", ""),
        )
        report.check(
            "健康检查不被缓存（Cache-Control: no-store）",
            "no-store" in live.headers.get("cache-control", ""),
            live.headers.get("cache-control", ""),
        )

        ready = client.get("/health/ready")
        ready_ok = ready.status_code == 200
        report.check(
            "GET /health/ready 返回 200（配置齐备 + 数据库可达）",
            ready_ok,
            (
                f"status={ready.status_code} checks={ready.json().get('checks')}"
                if ready_ok
                else f"status={ready.status_code} body={ready.text[:200]}"
            ),
        )
        if not ready_ok:
            report.check(
                "未就绪时返回统一错误结构（SERVICE_UNAVAILABLE）",
                _error_code(ready) == "SERVICE_UNAVAILABLE",
                _error_code(ready),
            )

        # ---------------- CORS ----------------
        if allowed_origin:
            allowed = client.options(
                "/api/v1/auth/login",
                headers={
                    "Origin": allowed_origin,
                    "Access-Control-Request-Method": "POST",
                },
            )
            report.check(
                f"允许来源 {allowed_origin} 的预检通过",
                allowed.headers.get("access-control-allow-origin") == allowed_origin,
                f"status={allowed.status_code} "
                f"acao={allowed.headers.get('access-control-allow-origin')}",
            )
            report.check(
                "CORS 允许携带凭据",
                allowed.headers.get("access-control-allow-credentials") == "true",
                allowed.headers.get("access-control-allow-credentials", ""),
            )

        foreign = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        report.check(
            "未授权来源拿不到 CORS 头",
            "access-control-allow-origin" not in foreign.headers,
            f"status={foreign.status_code}",
        )

        # ---------------- Auth 主流程 ----------------
        register = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": PASSWORD,
                "display_name": "验证账号",
                "role": "STUDENT",
            },
        )
        report.check(
            "POST /auth/register 返回 201",
            register.status_code == 201,
            f"status={register.status_code} body={register.text[:160]}",
        )
        if register.status_code == 201:
            body = register.json()
            report.check(
                "注册响应不含密码与令牌",
                "password" not in register.text and "token" not in register.text,
                str(sorted(body)),
            )
            report.check(
                "注册返回的 created_at 为 ISO 8601 UTC（以 Z 结尾）",
                str(body.get("created_at", "")).endswith("Z"),
                str(body.get("created_at")),
            )

        duplicate = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": PASSWORD,
                "display_name": "验证账号",
                "role": "STUDENT",
            },
        )
        report.check(
            "重复注册返回 409 AUTH_EMAIL_TAKEN",
            duplicate.status_code == 409 and _error_code(duplicate) == "AUTH_EMAIL_TAKEN",
            f"status={duplicate.status_code} code={_error_code(duplicate)}",
        )

        login = client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        report.check(
            "POST /auth/login 返回 200 与两个 Token",
            login.status_code == 200
            and {"access_token", "refresh_token"} <= set(login.json()),
            f"status={login.status_code}",
        )

        tokens: dict[str, str] = login.json() if login.status_code == 200 else {}
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")

        if access_token:
            report.check(
                "登录响应 expires_in 为 3600 秒",
                tokens.get("expires_in") == 3600,
                str(tokens.get("expires_in")),
            )
            me = client.get("/api/v1/users/me", headers=_auth(access_token))
            report.check(
                "GET /users/me 返回 200 且邮箱一致",
                me.status_code == 200 and me.json().get("email") == email,
                f"status={me.status_code}",
            )
            anonymous = client.get("/api/v1/users/me")
            report.check(
                "未携带令牌访问 /users/me 返回 401 AUTH_TOKEN_EXPIRED",
                anonymous.status_code == 401
                and _error_code(anonymous) == "AUTH_TOKEN_EXPIRED",
                f"status={anonymous.status_code} code={_error_code(anonymous)}",
            )

        if refresh_token:
            refreshed = client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            )
            refreshed_body = refreshed.json() if refreshed.status_code == 200 else {}
            report.check(
                "POST /auth/refresh 返回 200 且只含新的 Access Token",
                refreshed.status_code == 200
                and set(refreshed_body) == {"access_token", "token_type", "expires_in"},
                f"status={refreshed.status_code} keys={sorted(refreshed_body)}",
            )
            new_access = refreshed_body.get("access_token", "")
            report.check(
                "刷新返回的 Access Token 与原令牌不同",
                bool(new_access) and new_access != access_token,
                "",
            )
            report.check(
                "原 Refresh Token 仍可重复使用（不轮换）",
                client.post(
                    "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
                ).status_code
                == 200,
                "",
            )

            logout = client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": refresh_token},
                headers=_auth(new_access or access_token),
            )
            report.check(
                "POST /auth/logout 返回 204 且无响应体",
                logout.status_code == 204 and not logout.content,
                f"status={logout.status_code}",
            )
            revoked = client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            )
            report.check(
                "注销后原 Refresh Token 刷新返回 401",
                revoked.status_code == 401
                and _error_code(revoked) == "AUTH_TOKEN_EXPIRED",
                f"status={revoked.status_code} code={_error_code(revoked)}",
            )

        # 用独立邮箱验证错误凭证不泄露账号是否存在，避免影响上面的账号
        wrong_email = f"verify-wrong-{int(time.time())}@example.com"
        wrong_password = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "definitely wrong"}
        )
        unknown_account = client.post(
            "/api/v1/auth/login", json={"email": wrong_email, "password": PASSWORD}
        )
        report.check(
            "密码错误与账号不存在返回同一错误码与文案",
            wrong_password.status_code == unknown_account.status_code == 401
            and _error_code(wrong_password) == _error_code(unknown_account)
            and _error_message(wrong_password) == _error_message(unknown_account),
            f"{wrong_password.status_code}/{unknown_account.status_code} "
            f"{_error_code(wrong_password)}/{_error_code(unknown_account)}",
        )

    total = len(report.results)
    failed = report.failed
    print(f"\n结果: {total - len(failed)}/{total} 项通过")
    if failed:
        print("失败项:")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print("交付验证清单全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
