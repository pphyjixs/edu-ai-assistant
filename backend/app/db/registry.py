"""ORM 模型注册表。

Alembic 的 ``env.py`` 只依赖这一个模块来获取 ``target_metadata``：
新增业务模块时必须在这里导入其 ``models``，否则 autogenerate 看不到新表。
"""

from __future__ import annotations

from sqlalchemy import MetaData

from app.db.base import Base
from app.modules.auth.models import AuthSession, LoginAttempt, User
from app.modules.courses.models import Course, CourseMember

#: 供 Alembic 比较与自动生成迁移使用
target_metadata: MetaData = Base.metadata

__all__ = [
    "AuthSession",
    "Course",
    "CourseMember",
    "LoginAttempt",
    "User",
    "target_metadata",
]
