"""OpenAPI 辅助：为"手工解析请求体"的接口补回模型组件。

背景（契约 7.1 的错误优先级）：生成练习与提交答案这两个接口必须**先**完成
认证、资源可见性、角色、归档/状态检查并拿到行锁，**再**校验请求体——因此路由
只声明原始 :class:`~fastapi.Request`，由服务调用
:func:`app.core.request_body.parse_required_object_body` 解析。

代价是 FastAPI 不会再自动把这些模型收进 ``components.schemas``。本模块把这些
模型显式注册进去，并用 ``openapi_extra`` 里的 ``$ref`` 指向它们，保证导出的
OpenAPI 与契约文档一致（前端类型生成依赖它）。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

#: 组件引用模板，与 FastAPI 的默认命名一致
REF_TEMPLATE = "#/components/schemas/{model}"


def install_explicit_schemas(
    app: FastAPI, models: Sequence[type[BaseModel]]
) -> None:
    """把这些模型（及其嵌套定义）补进导出的 OpenAPI 组件。

    幂等：重复调用只会 ``setdefault``，不会覆盖 FastAPI 已生成的同名组件。
    """
    if not models:
        return

    base_openapi = app.openapi

    def custom_openapi() -> dict:
        schema = base_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        _top, definitions = models_json_schema(
            [(model, "validation") for model in models],
            ref_template=REF_TEMPLATE,
        )
        for name, definition in definitions.get("$defs", {}).items():
            components.setdefault(name, definition)
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


__all__ = ["REF_TEMPLATE", "install_explicit_schemas"]
