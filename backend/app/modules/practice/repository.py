"""Practice 数据访问层（``docs/api-contract.md`` 第 7 节）。

只做查询与写入，不含业务判断、不提交事务——事务边界在 ``service.py``
与 ``worker.py``。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.practice.models import (
    PracticeAttempt,
    PracticeAttemptAnswer,
    PracticeDifficulty,
    PracticeGenerationAttempt,
    PracticeGenerationStatus,
    PracticeQuestion,
    PracticeQuestionType,
    PracticeSet,
    PracticeSetMaterial,
    PracticeStatus,
)


async def list_generation_materials(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    material_ids: list[uuid.UUID],
) -> list:
    """生成前的资料检查：返回 ``(id, filename, status, chunk_count)``。

    只返回**属于本课程且未删除**的资料；调用方据此区分"不可见"（数量不符）
    与"未就绪/无片段"（状态或片段数不满足）。
    """
    from app.modules.materials.models import Material, MaterialChunk

    chunk_count = (
        select(func.count(MaterialChunk.id))
        .where(MaterialChunk.material_id == Material.id)
        .scalar_subquery()
    )
    result = await session.execute(
        select(
            Material.id,
            Material.filename,
            Material.status,
            chunk_count.label("chunk_count"),
        ).where(
            Material.id.in_(material_ids),
            Material.course_id == course_id,
            Material.deleted_at.is_(None),
        )
    )
    return list(result.all())


def create_set(
    session: AsyncSession,
    *,
    set_id: uuid.UUID,
    course_id: uuid.UUID,
    teacher_id: uuid.UUID,
    title: str,
    difficulty: PracticeDifficulty,
    requested_question_count: int,
    question_types: list[str],
    now: datetime,
) -> PracticeSet:
    """创建练习（``GENERATING``）；与生成任务在同一事务写入。"""
    practice_set = PracticeSet(
        id=set_id,
        course_id=course_id,
        teacher_id=teacher_id,
        title=title,
        status=PracticeStatus.GENERATING,
        difficulty=difficulty,
        requested_question_count=requested_question_count,
        question_count=0,
        question_types=question_types,
        created_at=now,
        updated_at=now,
    )
    session.add(practice_set)
    return practice_set


def add_set_material(
    session: AsyncSession,
    *,
    practice_set_id: uuid.UUID,
    material_id: uuid.UUID,
    order: int,
) -> PracticeSetMaterial:
    """记录生成时选择的来源资料及顺序。"""
    link = PracticeSetMaterial(
        id=uuid.uuid4(),
        practice_set_id=practice_set_id,
        material_id=material_id,
        order=order,
    )
    session.add(link)
    return link


async def list_set_material_ids(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> list[uuid.UUID]:
    """按选择顺序取来源资料 ID。"""
    result = await session.execute(
        select(PracticeSetMaterial.material_id)
        .where(PracticeSetMaterial.practice_set_id == practice_set_id)
        .order_by(PracticeSetMaterial.order)
    )
    return list(result.scalars().all())


async def get_set_by_id(
    session: AsyncSession, set_id: uuid.UUID
) -> PracticeSet | None:
    return await session.get(PracticeSet, set_id)


async def get_set_for_update(
    session: AsyncSession, set_id: uuid.UUID
) -> PracticeSet | None:
    """锁住练习行（发布、提交、Worker 回写的临界区）。"""
    result = await session.execute(
        select(PracticeSet)
        .where(PracticeSet.id == set_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_published_sets(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[PracticeSet], int]:
    """课程已发布练习（契约 7.3）：``published_at DESC, id DESC``。"""
    conditions = (
        PracticeSet.course_id == course_id,
        PracticeSet.status == PracticeStatus.PUBLISHED,
    )
    total = await session.scalar(
        select(func.count()).select_from(PracticeSet).where(*conditions)
    )
    result = await session.execute(
        select(PracticeSet)
        .where(*conditions)
        .order_by(PracticeSet.published_at.desc(), PracticeSet.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


def add_question(
    session: AsyncSession,
    *,
    question_id: uuid.UUID,
    practice_set_id: uuid.UUID,
    order: int,
    question_type: PracticeQuestionType,
    prompt: str,
    options: list[dict],
    correct_answer: object,
    explanation: str,
    knowledge_point: str | None,
    grading_points: list[dict],
    source_material_id: uuid.UUID,
    source_material_name: str,
    source_location_start: int,
    source_location_end: int,
    source_quote: str,
    now: datetime,
) -> PracticeQuestion:
    """暂存一道题目（Worker 成功路径，一次性写入全部题目）。"""
    question = PracticeQuestion(
        id=question_id,
        practice_set_id=practice_set_id,
        order=order,
        type=question_type,
        prompt=prompt,
        options=options,
        correct_answer=correct_answer,
        explanation=explanation,
        knowledge_point=knowledge_point,
        grading_points=grading_points,
        source_material_id=source_material_id,
        source_material_name=source_material_name,
        source_location_start=source_location_start,
        source_location_end=source_location_end,
        source_quote=source_quote,
        created_at=now,
    )
    session.add(question)
    return question


async def delete_questions(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> None:
    """清空练习的题目（重试前全量重写）。"""
    await session.execute(
        delete(PracticeQuestion).where(
            PracticeQuestion.practice_set_id == practice_set_id
        )
    )


async def list_questions(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> list[PracticeQuestion]:
    """按 ``order`` 升序取题目。"""
    result = await session.execute(
        select(PracticeQuestion)
        .where(PracticeQuestion.practice_set_id == practice_set_id)
        .order_by(PracticeQuestion.order)
    )
    return list(result.scalars().all())


def add_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    practice_set_id: uuid.UUID,
    student_id: uuid.UUID,
    total_score: Decimal,
    now: datetime,
) -> PracticeAttempt:
    """创建答题记录（与各题得分在同一事务写入）。"""
    attempt = PracticeAttempt(
        id=attempt_id,
        practice_set_id=practice_set_id,
        student_id=student_id,
        total_score=total_score,
        submitted_at=now,
    )
    session.add(attempt)
    return attempt


def add_attempt_answer(
    session: AsyncSession,
    *,
    answer_id: uuid.UUID,
    attempt_id: uuid.UUID,
    question_id: uuid.UUID,
    question_order: int,
    submitted_answer: object,
    is_correct: bool,
    score: Decimal,
    matched_points: list[str],
    now: datetime,
) -> PracticeAttemptAnswer:
    """暂存单题得分。"""
    answer = PracticeAttemptAnswer(
        id=answer_id,
        attempt_id=attempt_id,
        question_id=question_id,
        question_order=question_order,
        submitted_answer=submitted_answer,
        is_correct=is_correct,
        score=score,
        matched_points=matched_points,
        created_at=now,
    )
    session.add(answer)
    return answer


async def get_attempt_by_id(
    session: AsyncSession, attempt_id: uuid.UUID
) -> PracticeAttempt | None:
    return await session.get(PracticeAttempt, attempt_id)


async def get_attempt_for_student(
    session: AsyncSession, *, practice_set_id: uuid.UUID, student_id: uuid.UUID
) -> PracticeAttempt | None:
    """取某学生在某练习上的答题记录（唯一约束保证至多一条）。"""
    result = await session.execute(
        select(PracticeAttempt).where(
            PracticeAttempt.practice_set_id == practice_set_id,
            PracticeAttempt.student_id == student_id,
        )
    )
    return result.scalar_one_or_none()


async def list_attempt_answers(
    session: AsyncSession, *, attempt_id: uuid.UUID
) -> list[PracticeAttemptAnswer]:
    """按题目顺序取答题明细。"""
    result = await session.execute(
        select(PracticeAttemptAnswer)
        .where(PracticeAttemptAnswer.attempt_id == attempt_id)
        .order_by(PracticeAttemptAnswer.question_order)
    )
    return list(result.scalars().all())


def add_generation_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    practice_set_id: uuid.UUID,
    teacher_id: uuid.UUID,
    status: PracticeGenerationStatus,
    model: str | None,
    prompt_version: str,
    requested_count: int,
    question_count: int | None,
    duration_ms: int,
    error: str | None,
    now: datetime,
) -> PracticeGenerationAttempt:
    """写一条生成尝试记录（成功与失败都留痕）。"""
    attempt = PracticeGenerationAttempt(
        id=attempt_id,
        practice_set_id=practice_set_id,
        teacher_id=teacher_id,
        status=status,
        model=model,
        prompt_version=prompt_version,
        requested_count=requested_count,
        question_count=question_count,
        duration_ms=duration_ms,
        error=error,
        created_at=now,
    )
    session.add(attempt)
    return attempt


async def count_generation_attempts(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> int:
    """统计练习的生成尝试数量（验收用）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PracticeGenerationAttempt)
            .where(PracticeGenerationAttempt.practice_set_id == practice_set_id)
        )
        or 0
    )


async def list_chunks_for_materials(
    session: AsyncSession,
    *,
    material_ids: list[uuid.UUID],
) -> list[tuple[uuid.UUID, str, str, int, int, int]]:
    """按资料顺序取片段快照（Worker 出题上下文）。

    返回每行 ``(material_id, filename, chunk_id, order, location_start,
    location_end, content)``，只读取**未删除且 READY** 的资料。
    """
    from app.modules.materials.models import Material, MaterialChunk, MaterialStatus

    result = await session.execute(
        select(
            MaterialChunk.material_id,
            Material.filename,
            MaterialChunk.id.label("chunk_id"),
            MaterialChunk.order,
            MaterialChunk.location_start,
            MaterialChunk.location_end,
            MaterialChunk.content,
        )
        .join(Material, Material.id == MaterialChunk.material_id)
        .where(
            MaterialChunk.material_id.in_(material_ids),
            Material.status == MaterialStatus.READY,
            Material.deleted_at.is_(None),
        )
        .order_by(MaterialChunk.material_id, MaterialChunk.order)
    )
    return list(result.all())
