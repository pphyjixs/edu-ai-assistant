"""共享依赖（FastAPI ``Depends`` 注入点）。

按 ``docs/architecture.md``，跨模块的依赖注入统一收在 ``core`` 下，
业务模块只引用这里的类型别名，不各自重复声明。

配置从 ``app.state.settings`` 读取而不是直接调用
:func:`app.core.config.get_settings`，这样 :func:`app.main.create_app`
传入的配置（测试与多实例场景）才能真正生效，环境变量的读取只发生一次。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings


def get_app_settings(request: Request) -> Settings:
    """返回当前应用实例使用的配置。

    测试中可通过 ``app.dependency_overrides[get_app_settings]`` 覆盖。
    """
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    # 兜底：应用未通过 create_app 注入配置时退回环境变量
    return get_settings()


#: 注入应用配置
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
