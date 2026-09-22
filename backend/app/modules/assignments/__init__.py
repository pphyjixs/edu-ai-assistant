"""Assignments 模块：实验任务（``docs/api-contract.md`` 第 8 节）。

对外六个接口（创建、列表、详情、修改、发布、关闭），以及供第 9 节 Submission
模块复用的内部服务（"是否允许提交"与"当前评分规则版本"）：

- :mod:`app.modules.assignments.models`：三张表与状态枚举；
- :mod:`app.modules.assignments.schemas`：请求/响应模型与纯校验（总分、规则指纹）；
- :mod:`app.modules.assignments.repository`：数据访问（版本只追加）；
- :mod:`app.modules.assignments.service`：权限、状态机、版本策略与事务边界；
- :mod:`app.modules.assignments.deps`：写路径的加锁守卫依赖；
- :mod:`app.modules.assignments.router`：HTTP 协议层。

报告上传、提交、AI 批改、教师复核与成绩发布属于第 9 节，本轮未实现。
"""

from __future__ import annotations

__all__: list[str] = []
