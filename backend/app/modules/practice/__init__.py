"""Practice 模块：课程练习（``docs/api-contract.md`` 第 7 节）。

对外六个接口（生成、已发布列表、详情、发布、提交、结果）+ 独立生成
Worker + 任务查询与重试：

- :mod:`app.modules.practice.models`：六张表；
- :mod:`app.modules.practice.schemas`：请求/响应与模型输出校验；
- :mod:`app.modules.practice.repository`：数据访问；
- :mod:`app.modules.practice.generation_ai`：可替换的生成适配层；
- :mod:`app.modules.practice.scoring`：百分制评分；
- :mod:`app.modules.practice.service`：业务规则与事务边界；
- :mod:`app.modules.practice.worker`：领取、租约、回写；
- :mod:`app.modules.practice.router`：HTTP 协议层。
"""

from __future__ import annotations

__all__: list[str] = []
