"""对象存储的依赖注入点。

``StorageDep`` 让业务路由以依赖形式拿到适配器，测试可以用
``app.dependency_overrides[get_storage_dep]`` 换成内存假实现，
从而在不接触真实对象存储的前提下覆盖「对象缺失 / 元数据被篡改 / 存储不可用」等分支。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.deps import SettingsDep
from app.storage.s3 import S3Storage, get_storage


def get_storage_dep(settings: SettingsDep) -> S3Storage:
    """返回当前应用配置对应的存储适配器（进程内共享实例）。"""
    return get_storage(settings)


#: 注入对象存储适配器
StorageDep = Annotated[S3Storage, Depends(get_storage_dep)]
