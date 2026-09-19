"""业务模块包。

每个模块遵循 ``docs/architecture.md`` 第 4 节的内部结构：
``router.py`` / ``schemas.py`` / ``service.py`` / ``repository.py`` /
``models.py`` / ``permissions.py`` / ``tests/``。

模块之间不得直接读取对方的 ORM 表，跨模块调用走对方的 service。
"""

__all__: list[str] = []
