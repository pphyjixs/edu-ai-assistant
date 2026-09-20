"""Jobs 模块：统一管理资料解析、练习生成与报告批改的异步任务。

任务状态是各业务模块共用的基础设施：任务表不反向外键到具体业务表，
而是通过 ``(resource_type, resource_id)`` 泛化引用，由 ``(type, resource_id)``
唯一约束保证同一资源上同类任务只有一条（见 ``docs/modules.md`` 第 8 节）。
"""

__all__: list[str] = []
