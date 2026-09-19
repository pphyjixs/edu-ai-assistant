"""集成测试包。

跑在 **独立 PostgreSQL 测试库** 上，表结构由 Alembic 迁移创建
（夹具见 ``tests/conftest.py``、工具见 ``tests/pg_support.py``）。

未配置 ``DATABASE_URL`` 或实例不可达时，相关用例由夹具自动 skip。
"""

__all__: list[str] = []
