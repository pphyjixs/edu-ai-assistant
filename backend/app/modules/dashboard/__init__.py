"""Dashboard 模块：跨模块只读聚合（``docs/api-contract.md`` 第 11 节）。"""

from app.modules.dashboard.router import dashboard_router

__all__ = ["dashboard_router"]
