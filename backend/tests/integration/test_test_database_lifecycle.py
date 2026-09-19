"""测试库生命周期（真实 PostgreSQL）。

验证"专用库 + 结束后清理"这条规则真的在跑：

- 当前会话用的测试库必须是 ``_test`` 结尾；
- 守卫会拒绝把开发库 / 系统库当测试库操作；
- 自建的一次性库在用例结束后确实被删除。
"""

from __future__ import annotations

import pytest

from tests import pg_support


def test_session_uses_a_dedicated_test_database(pg_test_url: str) -> None:
    name = pg_support.database_name(pg_test_url)

    assert name.endswith(pg_support.TEST_SUFFIX)
    pg_support.assert_test_database_name(name)


def test_guard_refuses_to_touch_the_development_database(
    pg_test_url: str,
) -> None:
    """即使代码被误用，也不会去动非 ``_test`` 的库。"""
    admin = pg_support.admin_engine(pg_test_url)
    try:
        for unsafe in ("edu_ai_dev", "postgres", "template0"):
            with pytest.raises(pg_support.DatabaseNameConflict):
                pg_support.create_test_database(admin, unsafe)
            with pytest.raises(pg_support.DatabaseNameConflict):
                pg_support.drop_test_database(admin, unsafe)
    finally:
        admin.dispose()


def test_test_database_is_created_and_cleaned_up(pg_test_url: str) -> None:
    """用一次"建库 → 断言存在 → 删库 → 断言消失"验证清理逻辑。"""
    admin = pg_support.admin_engine(pg_test_url)
    decoy = f"{pg_support.database_name(pg_test_url).removesuffix(pg_support.TEST_SUFFIX)}_lifecycle_test"
    pg_support.assert_test_database_name(decoy)
    created = False
    try:
        pg_support.create_test_database(admin, decoy)
        created = True
        assert pg_support.database_exists(admin, decoy)

        pg_support.drop_test_database(admin, decoy)
        created = False
        assert not pg_support.database_exists(admin, decoy)
    finally:
        # 兜底清理，避免异常路径留下残留
        try:
            if created:
                pg_support.drop_test_database(admin, decoy)
        finally:
            admin.dispose()
