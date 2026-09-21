"""Chat 模块：课程问答（``docs/api-contract.md`` 第 6 节）。

对外四个接口（会话创建、会话列表、消息列表、发送问题）+ 受约束的
检索增强生成：

- :mod:`app.modules.chat.retrieval`：pg_trgm 检索（限定课程、``READY``、未删除）；
- :mod:`app.modules.chat.answer_ai`：可替换的模型适配层 + 引用校验；
- :mod:`app.modules.chat.service`：事务边界与并发控制；
- :mod:`app.modules.chat.router`：HTTP 协议层。
"""

from __future__ import annotations

__all__: list[str] = []
