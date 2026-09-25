"""迁移验收测试（专用一次性 PostgreSQL 库）。

对应 ``docs/deployment-vercel.md`` 第 8.2 节：在**全新数据库**上验证升级、模型一致性
与回滚。用例会自带一个 ``<库名>_migration_check`` 专用库（开始时重建、结束时删除），
因此即使同时跑其他集成测试也不会互相干扰。

数据格式与类型（``TIMESTAMPTZ``、原生枚举、约束命名）都在真实 PostgreSQL 上校验，
这是 SQLite 无法替代的部分。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from app.core.config import BACKEND_DIR
from app.db.registry import target_metadata
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

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
    "material_sections",
    "material_knowledge_points",
    "material_chunks",
    "material_delete_todos",
    "chat_sessions",
    "chat_messages",
    "chat_message_citations",
    "chat_generation_attempts",
    "practice_sets",
    "practice_set_materials",
    "practice_questions",
    "practice_attempts",
    "practice_attempt_answers",
    "practice_generation_attempts",
    "assignments",
    "assignment_attachments",
    "assignment_rubric_versions",
    "assignment_rubric_items",
    "agent_runs",
    "agent_run_sources",
    "agent_run_steps",
    "submissions",
    "submission_upload_sessions",
    "grade_reviews",
    "grade_items",
    "submission_grade_attempts",
}

#: 迁移引入的原生枚举类型，回滚时必须全部清理
EXPECTED_ENUMS: dict[str, list[str]] = {
    "user_role": ["TEACHER", "STUDENT"],
    "course_status": ["ACTIVE", "ARCHIVED"],
    "course_role": ["TEACHER", "STUDENT"],
    "job_type": ["MATERIAL_PARSE", "PRACTICE_GENERATE", "SUBMISSION_GRADE", "AGENT_RUN"],
    "job_status": ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"],
    # 0011 用 ALTER TYPE ADD VALUE 追加 AGENT_RUN，新值排在末尾
    "job_resource_type": ["MATERIAL", "PRACTICE_SET", "SUBMISSION", "AGENT_RUN"],
    "material_status": ["UPLOADING", "UPLOADED", "PROCESSING", "READY", "FAILED"],
    "material_delete_todo_status": ["PENDING", "DONE"],
    "chat_message_role": ["USER", "ASSISTANT"],
    "chat_attempt_status": ["SUCCEEDED", "FAILED"],
    "practice_status": ["GENERATING", "DRAFT", "PUBLISHED", "FAILED", "CANCELLED"],
    "practice_difficulty": ["EASY", "MEDIUM", "HARD"],
    "practice_question_type": ["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"],
    "practice_generation_status": ["SUCCEEDED", "FAILED"],
    "assignment_status": ["DRAFT", "PUBLISHED", "CLOSED", "ARCHIVED"],
    "agent_run_action": [
        "ASK",
        "SUMMARIZE_CONTEXT",
        "BREAK_DOWN_ASSIGNMENT",
        "CHECK_SUBMISSION",
    ],
    "agent_entity_type": [
        "COURSE",
        "MATERIAL",
        "MATERIAL_SECTION",
        "ASSIGNMENT",
        "SUBMISSION",
        "GRADE",
    ],
    "agent_source_type": ["COURSE", "MATERIAL_CHUNK", "MATERIAL_OUTLINE", "ASSIGNMENT"],
    "submission_status": [
        "UPLOADING",
        "SUBMITTED",
        "GRADING",
        "REVIEW_REQUIRED",
        "PUBLISHED",
        "FAILED",
    ],
    "submission_grade_attempt_status": ["SUCCEEDED", "FAILED"],
}

COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True}

#: head 对应的最新迁移
REVISION = "0016_agent_tool_steps"

#: jobs 的两条契约约束（库侧实际名字带双重前缀，见命名约定）
JOBS_PROGRESS_CONSTRAINT = "ck_jobs_ck_jobs_progress_range"
JOBS_TYPE_RESOURCE_CONSTRAINT = "ck_jobs_ck_jobs_type_resource_match"

#: 合法的「任务类型 / 资源类型」四种组合（含内部 AGENT_RUN）
LEGAL_JOB_COMBINATIONS = [
    ("MATERIAL_PARSE", "MATERIAL"),
    ("PRACTICE_GENERATE", "PRACTICE_SET"),
    ("SUBMISSION_GRADE", "SUBMISSION"),
    ("AGENT_RUN", "AGENT_RUN"),
]


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


def test_grade_items_reference_historical_rubric_items(migrated_engine: Engine) -> None:
    """批改按提交固定的评分版本评分：评分项外键指向历史 RubricItem（RESTRICT）。"""
    inspector = inspect(migrated_engine)
    grade_item_fks = {
        fk["name"]: fk for fk in inspector.get_foreign_keys("grade_items")
    }

    assert "fk_grade_items_rubric_item_id_rubric_items" in grade_item_fks
    assert "fk_grade_items_review_id_grade_reviews" in grade_item_fks
    grade_item_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("grade_items")
    }
    assert "uq_grade_items_review_rubric_item" in grade_item_unique

    submission_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("submissions")
    }
    # 一位学生对同一任务只能有一份提交：并发由这条约束兜住
    assert "uq_submissions_assignment_student" in submission_unique

    review_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("grade_reviews")
    }
    # 每份提交唯一一条批改记录：重复触发不会创建第二条
    assert "uq_grade_reviews_submission_id" in review_unique

    submission_fks = {
        fk["name"] for fk in inspector.get_foreign_keys("submissions")
    }
    # 提交固定评分版本是**历史外键**：删除版本必须被拒绝，不能静默丢历史
    assert "fk_submissions_rubric_version_id_versions" in submission_fks


def test_grade_items_score_check_constraints(migrated_engine: Engine) -> None:
    """分数范围约束必须在库侧生效（AI 与教师终稿都不能超出满分）。

    名称沿用项目统一的命名约定（``ck_%(table_name)s_%(constraint_name)s``）：
    模型与迁移里显式给出的名字本身已带 ``ck_`` 前缀，因此实际落库名是双前缀
    —— 与 assignments / practice 的既有约束完全一致，不能只在本模块"改漂亮"。
    """
    checks = {
        constraint["name"]
        for constraint in inspect(migrated_engine).get_check_constraints("grade_items")
    }

    assert "ck_grade_items_ck_grade_items_ai_score" in checks
    assert "ck_grade_items_ck_grade_items_final_score" in checks
    assert "ck_grade_items_ck_grade_items_max_score" in checks
    assert "ck_grade_items_ck_grade_items_order" in checks

    with migrated_engine.connect() as connection:
        score_checks = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE contype = 'c' AND conrelid = 'grade_items'::regclass"
                " AND conname LIKE '%ai_score'"
            )
        ).scalars().all()
    assert score_checks and "max_score" in score_checks[0]


def test_grade_items_evidence_constraints(migrated_engine: Engine) -> None:
    """证据定位的结构断言：四列齐备且非空，来源/位置/区间三条 CHECK 生效。

    对应修复计划第 2 类：证据必须"类型 + 位置 + 区间"可核验，库侧兜底。
    """
    inspector = inspect(migrated_engine)
    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("grade_items")
    }
    assert "ck_grade_items_ck_grade_items_evidence_source" in checks
    assert "ck_grade_items_ck_grade_items_evidence_start" in checks
    assert "ck_grade_items_ck_grade_items_evidence_range" in checks

    columns = {
        column["name"]: column for column in inspector.get_columns("grade_items")
    }
    for name in (
        "evidence_quote",
        "evidence_source_type",
        "evidence_location_start",
        "evidence_location_end",
    ):
        assert name in columns, f"grade_items 缺少证据列 {name}"
        assert columns[name]["nullable"] is False, f"证据列 {name} 必须非空"


def test_upload_session_partial_unique_index(migrated_engine: Engine) -> None:
    """同一提交同时最多一个"已完成"的上传会话：部分唯一索引必须带谓词。"""
    inspector = inspect(migrated_engine)
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("submission_upload_sessions")
    }
    partial = indexes.get("uq_submission_upload_sessions_submission_completed")
    assert partial is not None, "缺少完成态上传会话的部分唯一索引"
    assert partial["unique"] is True
    assert partial["column_names"] == ["submission_id"]

    with migrated_engine.connect() as connection:
        indexdef = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname = 'uq_submission_upload_sessions_submission_completed'"
            )
        ).scalar_one()
    # 部分索引的谓词：只约束"已完成"的会话，进行中的会话可以并存
    assert "WHERE" in indexdef and "completed_at IS NOT NULL" in indexdef


def test_upgrade_downgrade_round_trip(
    alembic_config: Config, migration_database_url: str
) -> None:
    """``0012 → 0013 → 0012 → 0013`` 往返必须成功。

    覆盖契约 10 的迁移验收与 Agent → Grading → Jobs 的相邻迁移边界：
    0013 只增删两条 CHECK 约束，降级回 ``0012_submissions_grading`` 后
    Agent 两表与第 9 节的五张表都必须保留，约束全部消失。
    结束状态为 ``head``，因此可以安全地作为后续用例的起点。
    """
    engine = create_engine(pg_support.to_sync(migration_database_url))
    try:
        with pg_support.alembic_database_url(migration_database_url):
            command.downgrade(alembic_config, "0012_submissions_grading")
            after_downgrade = set(inspect(engine).get_table_names())
            assert "agent_runs" in after_downgrade
            assert "submissions" in after_downgrade
            assert "grade_reviews" in after_downgrade

            remaining = {
                constraint["name"]
                for constraint in inspect(engine).get_check_constraints("jobs")
            }
            assert JOBS_PROGRESS_CONSTRAINT not in remaining
            assert JOBS_TYPE_RESOURCE_CONSTRAINT not in remaining

            command.upgrade(alembic_config, "head")

        assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
        restored = {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("jobs")
        }
        assert JOBS_PROGRESS_CONSTRAINT in restored
        assert JOBS_TYPE_RESOURCE_CONSTRAINT in restored
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == REVISION
    finally:
        engine.dispose()


def _insert_job(
    connection, *, job_type: str, resource_type: str, progress: int = 0
) -> None:
    """向 ``jobs`` 插入一行（列名与类型来自测试常量）。"""
    connection.execute(
        text(
            "INSERT INTO jobs"
            " (id, type, status, progress, resource_type, resource_id, created_at)"
            " VALUES (CAST(:id AS uuid), CAST(:job_type AS job_type),"
            " 'PENDING'::job_status, :progress,"
            " CAST(:resource_type AS job_resource_type),"
            " CAST(:resource_id AS uuid), now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "job_type": job_type,
            "progress": progress,
            "resource_type": resource_type,
            "resource_id": str(uuid.uuid4()),
        },
    )


def test_jobs_check_constraints_are_enforced(migrated_engine: Engine) -> None:
    """进度越界与类型/资源不匹配在库侧被拒绝（契约 10.0）。"""
    inspector = inspect(migrated_engine)
    names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("jobs")
    }
    assert JOBS_PROGRESS_CONSTRAINT in names
    assert JOBS_TYPE_RESOURCE_CONSTRAINT in names

    for progress in (-1, 101):
        with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
            _insert_job(
                connection,
                job_type="MATERIAL_PARSE",
                resource_type="MATERIAL",
                progress=progress,
            )

    mismatches = [
        ("PRACTICE_GENERATE", "SUBMISSION"),
        ("SUBMISSION_GRADE", "PRACTICE_SET"),
        ("AGENT_RUN", "MATERIAL"),
        ("MATERIAL_PARSE", "AGENT_RUN"),
    ]
    for job_type, resource_type in mismatches:
        with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
            _insert_job(
                connection,
                job_type=job_type,
                resource_type=resource_type,
            )

    # 四种合法组合（含内部 AGENT_RUN）都能写入
    with migrated_engine.begin() as connection:
        for job_type, resource_type in LEGAL_JOB_COMBINATIONS:
            _insert_job(connection, job_type=job_type, resource_type=resource_type)
        stored = connection.execute(
            text(
                "SELECT count(*) FROM jobs WHERE type IN"
                " ('MATERIAL_PARSE', 'PRACTICE_GENERATE', 'SUBMISSION_GRADE',"
                " 'AGENT_RUN')"
            )
        ).scalar_one()
    assert stored == len(LEGAL_JOB_COMBINATIONS)


def test_dashboard_indexes_exist(migrated_engine: Engine) -> None:
    """0015 为 Dashboard 热路径建立 5 个索引（3 个部分索引），列顺序与谓词正确。"""
    inspector = inspect(migrated_engine)

    submission_indexes = {
        index["name"]: index for index in inspector.get_indexes("submissions")
    }
    assert submission_indexes["ix_submissions_course_submitted_id"]["column_names"] == [
        "course_id",
        "submitted_at",
        "id",
    ]
    assert submission_indexes["ix_submissions_student_status_updated_id"]["column_names"] == [
        "student_id",
        "status",
        "updated_at",
        "id",
    ]

    assignment_indexes = {
        index["name"]: index for index in inspector.get_indexes("assignments")
    }
    assert assignment_indexes["ix_assignments_course_status_due_id"]["column_names"] == [
        "course_id",
        "status",
        "due_at",
        "id",
    ]

    material_indexes = {
        index["name"]: index for index in inspector.get_indexes("materials")
    }
    assert material_indexes["ix_materials_course_status_updated_id"]["column_names"] == [
        "course_id",
        "status",
        "updated_at",
        "id",
    ]

    review_indexes = {
        index["name"]: index for index in inspector.get_indexes("grade_reviews")
    }
    assert review_indexes["ix_grade_reviews_published_id_submission_id"]["column_names"] == [
        "published_at",
        "id",
        "submission_id",
    ]

    with migrated_engine.connect() as connection:
        def indexdef(name: str) -> str:
            return connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": name},
            ).scalar_one()

        assert "submitted_at IS NOT NULL" in indexdef(
            "ix_submissions_course_submitted_id"
        )
        assert "deleted_at IS NULL" in indexdef("ix_materials_course_status_updated_id")
        assert "published_at IS NOT NULL" in indexdef(
            "ix_grade_reviews_published_id_submission_id"
        )


def test_dashboard_indexes_round_trip(
    alembic_config: Config, migration_database_url: str
) -> None:
    """``01d9328a578b → 0015 → 01d9328a578b → 0015`` 往返：只增删索引，不动表、枚举与历史数据。"""
    engine = create_engine(pg_support.to_sync(migration_database_url))
    try:
        with pg_support.alembic_database_url(migration_database_url):
            command.downgrade(alembic_config, "01d9328a578b")
            remaining = {
                index["name"] for index in inspect(engine).get_indexes("submissions")
            }
            assert "ix_submissions_course_submitted_id" not in remaining
            assert "ix_submissions_student_status_updated_id" not in remaining
            assert "ix_assignments_course_status_due_id" not in {
                index["name"] for index in inspect(engine).get_indexes("assignments")
            }
            assert "ix_materials_course_status_updated_id" not in {
                index["name"] for index in inspect(engine).get_indexes("materials")
            }
            assert "ix_grade_reviews_published_id_submission_id" not in {
                index["name"] for index in inspect(engine).get_indexes("grade_reviews")
            }

            command.upgrade(alembic_config, "head")

        assert "ix_submissions_course_submitted_id" in {
            index["name"] for index in inspect(engine).get_indexes("submissions")
        }
        assert "ix_materials_course_status_updated_id" in {
            index["name"] for index in inspect(engine).get_indexes("materials")
        }
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == REVISION
    finally:
        engine.dispose()


def test_agent_tool_steps_round_trip(
    alembic_config: Config, migration_database_url: str
) -> None:
    """``0015 → 0016 → 0015 → 0016`` 往返：只增删审计表与编排器版本列。

    0016（开发方案 7.1）为工具循环新增 ``agent_run_steps`` 与
    ``agent_runs.orchestrator_version``，不修改任何既有行的语义，
    因此降级后必须回到与 0015 完全一致的结构。
    """
    engine = create_engine(pg_support.to_sync(migration_database_url))
    try:
        with pg_support.alembic_database_url(migration_database_url):
            command.downgrade(alembic_config, "0015_dashboard_indexes")

            assert "agent_run_steps" not in inspect(engine).get_table_names()
            assert "orchestrator_version" not in {
                column["name"] for column in inspect(engine).get_columns("agent_runs")
            }

            command.upgrade(alembic_config, "head")

        inspector = inspect(engine)
        assert "agent_run_steps" in inspector.get_table_names()
        assert "orchestrator_version" in {
            column["name"] for column in inspector.get_columns("agent_runs")
        }

        # 两条唯一约束各司其职：步骤序号连续、call_id 幂等
        constraints = {
            constraint["name"] for constraint in inspector.get_unique_constraints("agent_run_steps")
        }
        assert "uq_agent_run_steps_run_order" in constraints
        assert "uq_agent_run_steps_run_call" in constraints
        assert "ix_agent_run_steps_run_order" in {
            index["name"] for index in inspector.get_indexes("agent_run_steps")
        }
        check_names = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("agent_run_steps")
        }
        # 命名约定会给 CHECK 再加一次 ck_<table>_ 前缀（与 jobs 的两条约束同理），
        # 因此库侧实际名字是双前缀；ORM 里声明的是短名。
        assert "ck_agent_run_steps_ck_agent_run_steps_request_object" in check_names

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == REVISION
    finally:
        engine.dispose()


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
