"""创建失败时必须保留目标库；只清理本次成功创建的数据库。"""

from unittest.mock import Mock

import pytest

from tests import conftest, pg_support
from tests.integration.test_migrations import migration_database_url


@pytest.fixture
def database_calls(monkeypatch):
    url = "postgresql+asyncpg://tester:placeholder@localhost/fixture_test"
    admin = Mock()
    create = Mock()
    drop = Mock()
    upgrade = Mock()
    monkeypatch.setattr(pg_support, "resolve_test_database_url", lambda: url)
    monkeypatch.setattr(pg_support, "is_postgres_reachable", lambda _: True)
    monkeypatch.setattr(pg_support, "configured_base_name", lambda: "fixture_dev")
    monkeypatch.setattr(pg_support, "admin_engine", lambda _: admin)
    monkeypatch.setattr(pg_support, "create_test_database", create)
    monkeypatch.setattr(pg_support, "drop_test_database", drop)
    monkeypatch.setattr(pg_support, "upgrade_head", upgrade)
    return url, admin, create, drop, upgrade


def fixture_generator(kind, url):
    if kind == "integration":
        return conftest.pg_test_url.__wrapped__()
    return migration_database_url.__wrapped__(url)


@pytest.mark.parametrize("kind", ["integration", "migration"])
def test_create_conflict_never_drops_existing_database(kind, database_calls):
    url, admin, create, drop, _ = database_calls
    create.side_effect = pg_support.DatabaseNameConflict("database in use")

    with pytest.raises(pg_support.DatabaseNameConflict, match="database in use"):
        next(fixture_generator(kind, url))

    drop.assert_not_called()
    admin.dispose.assert_called_once()


@pytest.mark.parametrize("kind", ["integration", "migration"])
def test_owned_database_is_cleaned_up_after_use(kind, database_calls):
    url, admin, _, drop, _ = database_calls
    fixture = fixture_generator(kind, url)
    next(fixture)
    drop.assert_not_called()

    fixture.close()

    drop.assert_called_once()
    admin.dispose.assert_called_once()


def test_migration_failure_still_cleans_up_owned_database(database_calls):
    url, admin, _, drop, upgrade = database_calls
    upgrade.side_effect = RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        next(fixture_generator("integration", url))

    drop.assert_called_once_with(admin, "fixture_test")
    admin.dispose.assert_called_once()
