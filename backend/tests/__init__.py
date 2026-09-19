"""后端测试包。

分层约定（见 docs/architecture.md）：

- ``unit/``        纯逻辑与单端点测试，外部依赖全部替换为 fake。
- ``integration/`` 覆盖数据库、存储等真实依赖的测试。
- ``contract/``    与 docs/api-contract.md 逐条对应的契约测试。
"""

__all__: list[str] = []
