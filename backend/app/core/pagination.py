"""分页：统一的查询参数与响应包装（``docs/api-contract.md`` 第 1 节）。

- ``page`` 从 1 开始，``page_size`` 默认 20、最大 100；越界直接 422，
  不做静默截断，避免前端拿到与请求不一致的分页结果。
- 响应固定为 ``{items, page, page_size, total}``；``items`` 为空数组而不是 ``null``。

所有列表接口共用这一套，保证课程、资料、任务等模块的分页行为一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel

#: 默认每页条数
DEFAULT_PAGE_SIZE = 20

#: 每页条数上限
MAX_PAGE_SIZE = 100

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    """分页响应。"""

    items: list[ItemT]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """已校验的分页参数，附带数据库查询需要的 offset / limit。"""

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def pagination_params(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}")
    ] = DEFAULT_PAGE_SIZE,
) -> PaginationParams:
    """解析分页查询参数。"""
    return PaginationParams(page=page, page_size=page_size)


#: 注入已校验的分页参数
PaginationDep = Annotated[PaginationParams, Depends(pagination_params)]
