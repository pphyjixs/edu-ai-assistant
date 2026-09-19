"""ORM 基类与元数据。

所有业务模块的 ORM 模型都继承 :class:`Base`，共用同一份 ``MetaData``，
Alembic 的 autogenerate 与迁移命名都依赖它。

命名约定是必须的：PostgreSQL 会自动为 CHECK/UNIQUE 等约束起名，若不统一命名，
自动生成的迁移在不同环境下会产生不一致的 ``ALTER`` 语句。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: 约束与索引的统一命名模板
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
