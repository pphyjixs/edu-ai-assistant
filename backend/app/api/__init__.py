"""HTTP 层：版本化路由聚合与进程级健康检查。

- :mod:`app.api.v1` 汇总所有业务模块路由，统一挂载在 ``/api/v1``。
- :mod:`app.api.health` 提供 ``/health/live`` 与 ``/health/ready``，
  按部署文档约定位于 ``/api/v1`` 之外，供平台探针直接访问。
"""

__all__: list[str] = []
