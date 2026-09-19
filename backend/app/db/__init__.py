"""数据库连接与会话管理。

本包只负责引擎/会话的创建与探测，ORM 基类与模型由各业务模块自行维护。
启动过程不建表、不执行迁移（见 docs/deployment-vercel.md）。
"""

__all__: list[str] = []
