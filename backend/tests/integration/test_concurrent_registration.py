"""并发重复注册验收测试（真实 PostgreSQL + 真实并发）。

对应 ``docs/acceptance.md`` 第 2 节"并发注册不能绕过数据库唯一约束"。

这里用 ``httpx.AsyncClient`` + ``asyncio.gather`` 让多个注册请求真正同时在飞，
而不是顺序调用——顺序调用只能证明"第二次被拦"，证明不了并发窗口下的行为。
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import Engine, text

REGISTER_URL = "/api/v1/auth/register"

#: 并发请求数；要大于 1 才能覆盖"两个请求同时通过占用检查"的窗口
CONCURRENCY = 8

PASSWORD = "Demo password 2026!"

EMAIL = "Racer@EXAMPLE.com"
NORMALIZED_EMAIL = "Racer@example.com"


def _payload(email: str = EMAIL) -> dict[str, str]:
    return {
        "email": email,
        "password": PASSWORD,
        "display_name": "并发测试",
        "role": "STUDENT",
    }


def _count_users(engine: Engine, email_normalized: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM users WHERE email_normalized = :name"),
                {"name": email_normalized},
            ).scalar_one()
        )


async def test_concurrent_registration_creates_exactly_one_account(
    async_api_client: httpx.AsyncClient, pg_sync_engine: Engine
) -> None:
    """同一比较值的并发注册：只有 1 个成功，其余都是 409，库里只有一行。"""
    responses = await asyncio.gather(
        *(async_api_client.post(REGISTER_URL, json=_payload()) for _ in range(CONCURRENCY))
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, *([409] * (CONCURRENCY - 1))], statuses

    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] == "AUTH_EMAIL_TAKEN"
        else:
            assert response.status_code == 201

    assert _count_users(pg_sync_engine, NORMALIZED_EMAIL) == 1


async def test_concurrent_registration_with_domain_case_variants(
    async_api_client: httpx.AsyncClient, pg_sync_engine: Engine
) -> None:
    """域名大小写不同的写法指向同一账号，因此同样只能成功一个。"""
    variants = [
        "Racer@EXAMPLE.com",
        "Racer@example.COM",
        "Racer@Example.com",
        "Racer@EXAMPLE.COM",
    ]

    responses = await asyncio.gather(
        *(async_api_client.post(REGISTER_URL, json=_payload(email)) for email in variants)
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409, 409, 409], statuses
    assert _count_users(pg_sync_engine, NORMALIZED_EMAIL) == 1


async def test_concurrent_registration_of_distinct_emails_all_succeed(
    async_api_client: httpx.AsyncClient, pg_sync_engine: Engine
) -> None:
    """对照组：互不相同的邮箱并发注册应当全部成功。"""
    emails = [f"racer{i}@example.com" for i in range(CONCURRENCY)]

    responses = await asyncio.gather(
        *(async_api_client.post(REGISTER_URL, json=_payload(email)) for email in emails)
    )

    assert [response.status_code for response in responses] == [201] * CONCURRENCY
    with pg_sync_engine.connect() as connection:
        total = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert total == CONCURRENCY
