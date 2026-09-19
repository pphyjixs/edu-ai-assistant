"""测试库配置守卫的单元测试（不连数据库）。

覆盖 ``docs/acceptance.md`` 第 12 节的测试库规则：

- 只设置 ``TEST_DATABASE_URL`` 也能跑（不要求 ``DATABASE_URL``）；
- 只允许 ``_test`` 结尾的专用库；
- 与开发库或系统库同名会被拒绝。
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from tests import pg_support


@pytest.fixture(autouse=True)
def _restore_settings_cache() -> None:
    """改动环境变量后恢复配置缓存，避免影响其他用例。"""
    yield
    get_settings.cache_clear()


def test_accepts_test_suffixed_name() -> None:
    assert pg_support.assert_test_database_name("edu_ai_dev_test") == "edu_ai_dev_test"


@pytest.mark.parametrize(
    "name",
    [
        "edu_ai_dev",  # 开发库本身：绝不能当测试库
        "prod_data",  # 非 _test 结尾
        "postgres",  # 系统库
        "template1",
        "bad-name",  # 非法字符
        "",  # 空
        "a" * 60,  # 超长
    ],
)
def test_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(pg_support.DatabaseNameConflict):
        pg_support.assert_test_database_name(name)


def test_rejects_name_that_equals_source_database() -> None:
    """派生结果与原库同名时立即报错，不能拿开发库跑测试。"""
    with pytest.raises(pg_support.DatabaseNameConflict):
        pg_support.assert_test_database_name("edu_ai_dev_test", base_name="edu_ai_dev_test")


def test_migration_database_name_has_its_own_suffix() -> None:
    assert pg_support.assert_test_database_name(
        "edu_ai_dev_migration_check", suffix=pg_support.MIGRATION_SUFFIX
    ) == "edu_ai_dev_migration_check"
    # 迁移库名不满足 _test 后缀，因此不能当作集成测试库
    with pytest.raises(pg_support.DatabaseNameConflict):
        pg_support.assert_test_database_name("edu_ai_dev_migration_check")


def test_resolve_prefers_explicit_test_database_url(monkeypatch) -> None:
    """只设置 TEST_DATABASE_URL 也必须能解析成功。"""
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql://user:pwd@127.0.0.1:5432/ci_suite_test"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()

    url = pg_support.resolve_test_database_url()

    assert url.startswith("postgresql+asyncpg://")
    assert url.endswith("/ci_suite_test")


def test_resolve_derives_test_name_from_database_url(monkeypatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pwd@127.0.0.1:5432/edu_ai_dev")
    get_settings.cache_clear()

    url = pg_support.resolve_test_database_url()

    assert pg_support.database_name(url) == "edu_ai_dev_test"


def test_resolve_raises_when_nothing_is_configured(monkeypatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()

    with pytest.raises(Exception):  # noqa: B017 - 连接串校验会抛错，夹具会转成 skip
        pg_support.resolve_test_database_url()


def test_derive_migration_database_name() -> None:
    url = "postgresql://user:pwd@127.0.0.1:5432/edu_ai_dev_test"

    assert pg_support.derive_migration_database_name(url) == "edu_ai_dev_migration_check"


def test_derived_names_always_guard_safe() -> None:
    """派生出来的名字一定能通过守卫（否则测试根本跑不起来）。"""
    url = "postgresql://user:pwd@127.0.0.1:5432/edu_ai_dev"

    test_url = pg_support.with_database(url, pg_support.derive_database_name(url, pg_support.TEST_SUFFIX))
    migration_name = pg_support.derive_migration_database_name(test_url)

    pg_support.assert_test_database_name(pg_support.database_name(test_url))
    pg_support.assert_test_database_name(
        migration_name, suffix=pg_support.MIGRATION_SUFFIX
    )
