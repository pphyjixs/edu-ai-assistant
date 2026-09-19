"""契约测试包。

逐条对照 ``docs/api-contract.md`` 校验请求与响应格式（字段名、类型、
状态码、传输方式）。

刻意 **不检查** 前端把令牌存在哪里：契约 2.2 明确规定"由前端自行选择存放位置
和内部键名，API 契约不作限制"，因此这里只断言传输协议本身（JSON 响应体 +
``Authorization: Bearer`` 头），不假设也不要求 Cookie 或 localStorage。
"""

__all__: list[str] = []
