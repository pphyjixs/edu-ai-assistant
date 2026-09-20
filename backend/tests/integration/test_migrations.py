"""迁移验收测试（专用一次性 PostgreSQL 库）。

对应 ``docs/deployment-vercel.md`` 第 8.2 节：在**全新数据库**上验证升级、模型一致性
与回滚。用例会自带一个 ``<库名>_migration_check`` 专用库（开始时重建、结束时删除），
因此即使同时跑其他集成测试也不会互相干扰。

数据格式与类型（``TIMESTAMPTZ``、原生枚举、约束命名）都在真实 PostgreSQL 上校验，
这是 SQLite 无法替代的部分。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from app.core.config import BACKEND_DIR
from app.db.registry import target_metadata
from tests import pg_support

EXPECTED_TABLES = {
    "users",
    "auth_sessions",
    "login_attempts",
    "courses",
    "course_members",
    "jobs",
    "material_upload_sessions",
    "materials",
}

#: 迁移引入的原生枚举类型，回滚时必须全部清理
EXPECTED_ENUMS: dict[str, list[str]] = {
    "user_role": ["TEACHER", "STUDENT"],
    "course_status": ["ACTIVE", "ARCHIVED"],
    "course_role": ["TEACHER", "STUDENT"],
    "job_type": ["MATERIAL_PARSE", "PRACTICE_GENERATE", "SUBMISSION_GRADE"],
    "job_status": ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"],
    "job_resource_type": ["MATERIAL", "PRACTICE_SET", "SUBMISSION"],
    "material_status": ["UPLOADING", "UPLOADED", "PROCESSING", "READY", "FAILED"],
}

COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True}

#: head 对应的最新迁移
REVISION = "0006_deletion_pipeline"


@pytest.fixture(scope="module")
def migration_database_url(pg_test_url: str) -> Iterator[str]:
    """专用的一次性迁移库：开始时重建（保证"全新"），结束时删除。

    由测试库名派生（``x_test`` → ``x_migration_check``），同样受后缀守卫保护，
    不会误删开发库。
    """
    name = pg_support.derive_migration_database_name(pg_test_url)
    admin = pg_support.admin_engine(pg_test_url)
    created = False
    try:
        pg_support.create_test_database(
            admin, name, suffix=pg_support.MIGRATION_SUFFIX
        )
        created = True
        yield pg_support.with_database(pg_test_url, name)
    finally:
        try:
            if created:
                pg_support.drop_test_database(
                    admin, name, suffix=pg_support.MIGRATION_SUFFIX
                )
        finally:
            admin.dispose()


@pytest.fixture(scope="module")
def alembic_config(migration_database_url: str) -> Iterator[Config]:
    """把 ``migrations/env.py`` 的目标库切到一次性迁移库。"""
    with pg_support.alembic_database_url(migration_database_url):
        yield Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture(scope="module")
def migrated_engine(
    alembic_config: Config, migration_database_url: str
) -> Iterator[Engine]:
    """在全新库上执行 ``upgrade head``，返回同步引擎供结构断言。"""
    with pg_support.alembic_database_url(migration_database_url):
        command.upgrade(alembic_config, "head")

    engine = create_engine(pg_support.to_sync(migration_database_url))
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_creates_expected_tables(migrated_engine: Engine) -> None:
    assert EXPECTED_TABLES <= set(inspect(migrated_engine).get_table_names())


def test_alembic_version_is_recorded(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        rows = (
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
        )

    assert rows == [REVISION]


def test_migration_matches_orm_models(migrated_engine: Engine) -> None:
    """迁移执行后的库结构必须与 ORM 模型完全一致，防止改了模型忘记写迁移。"""
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection=connection, opts=COMPARE_OPTIONS
        )
        diff = compare_metadata(context, target_metadata)

    assert diff == [], f"迁移与 ORM 模型不一致，请补充迁移：{diff}"


def test_email_unique_constraint_exists(migrated_engine: Engine) -> None:
    unique_names = {
        constraint["name"]
        for constraint in inspect(migrated_engine).get_unique_constraints("users")
    }

    assert "uq_users_email_normalized" in unique_names


def test_timestamp_columns_use_timestamptz(migrated_engine: Engine) -> None:
    """所有时间列必须是 ``timestamp with time zone``（应用侧统一写 UTC）。"""
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name, data_type"
                " FROM information_schema.columns"
                " WHERE table_schema = 'public'"
                " AND (column_name LIKE '%\\_at' OR column_name = 'failed_at')"
            )
        ).all()

    assert rows, "应当存在时间列"
    for row in rows:
        assert row.data_type == "timestamp with time zone", row


def test_role_column_is_native_enum(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        udt = connection.execute(
            text(
                "SELECT udt_name FROM information_schema.columns"
                " WHERE table_name = 'users' AND column_name = 'role'"
            )
        ).scalar_one()
        labels = (
            connection.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
                    " WHERE t.typname = :name ORDER BY e.enumsortorder"
                ),
                {"name": udt},
            )
            .scalars()
            .all()
        )

    assert udt == "user_role"
    assert labels == ["TEACHER", "STUDENT"]


def test_all_migration_enums_are_native(migrated_engine: Engine) -> None:
    """迁移引入的每个枚举类型都存在，且取值与 ORM 枚举一致。"""
    with migrated_engine.connect() as connection:
        for type_name, expected_labels in EXPECTED_ENUMS.items():
            labels = (
                connection.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum e"
                        " JOIN pg_type t ON t.oid = e.enumtypid"
                        " WHERE t.typname = :name ORDER BY e.enumsortorder"
                    ),
                    {"name": type_name},
                )
                .scalars()
                .all()
            )
            assert labels == expected_labels, f"{type_name} 枚举取值不一致"


def test_course_unique_constraints_exist(migrated_engine: Engine) -> None:
    """邀请码全局唯一 + 同一课程内用户唯一，是并发场景的唯一防线。"""
    inspector = inspect(migrated_engine)

    course_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("courses")
    }
    member_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("course_members")
    }

    assert "uq_courses_invite_code" in course_unique
    assert "uq_course_members_course_id_user_id" in member_unique


def test_material_unique_constraints_exist(migrated_engine: Engine) -> None:
    """一次上传只对应一条资料、一个解析任务，靠这两条唯一约束兜底。"""
    inspector = inspect(migrated_engine)

    material_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("materials")
    }
    session_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("material_upload_sessions")
    }
    job_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("jobs")
    }

    assert "uq_materials_upload_id" in material_unique
    assert "uq_material_upload_sessions_object_key" in session_unique
    assert "uq_jobs_type_resource_id" in job_unique


def test_upload_session_completion_foreign_key_exists(migrated_engine: Engine) -> None:
    """会话指向完成结果的循环外键必须在建表后补上（ORM 的 use_alter=True）。"""
    inspector = inspect(migrated_engine)
    names = {
        fk["name"]
        for fk in inspector.get_foreign_keys("material_upload_sessions")
    }

    assert "fk_material_upload_sessions_completed_material_id_materials" in names
    assert "fk_materials_upload_id_material_upload_sessions" in {
        fk["name"] for fk in inspector.get_foreign_keys("materials")
    }


def test_upload_session_has_expired_at_column(migrated_engine: Engine) -> None:
    """过期清理标记列存在（契约 4.6 的清理命令据此幂等）。"""
    columns = {
        column["name"]
        for column in inspect(migrated_engine).get_columns("material_upload_sessions")
    }
    assert "expired_at" in columns


def test_rollback_to_base_removes_every_schema_object(
    alembic_config: Config, migration_database_url: str
) -> None:
    """回滚必须彻底：表、索引与枚举类型都不能残留。

    枚举类型不会随表一起删除，``downgrade`` 里必须显式清理；否则再次
    ``upgrade`` 会因 ``type user_role already exists`` 失败。

    本用例放在文件最后：它会清空整个 schema，结束后再复位到 head。
    """
    engine = create_engine(pg_support.to_sync(migration_database_url))
    try:
        with pg_support.alembic_database_url(migration_database_url):
            command.downgrade(alembic_config, "base")

        assert EXPECTED_TABLES.isdisjoint(set(inspect(engine).get_table_names()))
        with engine.connect() as connection:
            leftover = (
                connection.execute(
                    text(
                        "SELECT typname FROM pg_type WHERE typname = ANY(:names)"
                    ),
                    {"names": list(EXPECTED_ENUMS)},
                )
                .scalars()
                .all()
            )
        assert not leftover, f"回滚后不应残留枚举类型：{leftover}"

        # 复位并确认可以重新建起来（等价于"全新数据库"的路径）
        with pg_support.alembic_database_url(migration_database_url):
            command.upgrade(alembic_config, "head")
        assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
