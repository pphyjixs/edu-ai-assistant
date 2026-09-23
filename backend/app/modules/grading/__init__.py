"""提交与批改模块（``docs/api-contract.md`` 第 9 节）。

对外提供：报告直传与提交查询、异步 AI 批改、教师复核与成绩发布。

子模块：

- :mod:`app.modules.grading.models`：五张表与提交状态机取值域；
- :mod:`app.modules.grading.schemas`：请求/响应模型与纯校验；
- :mod:`app.modules.grading.repository`：数据访问（不含业务判断、不提交事务）；
- :mod:`app.modules.grading.service`：业务规则与事务边界；
- :mod:`app.modules.grading.deps`：写接口的两阶段守卫（资源检查 + 行锁）；
- :mod:`app.modules.grading.router`：八个 HTTP 接口；
- :mod:`app.modules.grading.grading_ai`：AI 适配层与服务端输出校验；
- :mod:`app.modules.grading.worker`：``SUBMISSION_GRADE`` 任务执行器。
"""

from __future__ import annotations

__all__: list[str] = []
