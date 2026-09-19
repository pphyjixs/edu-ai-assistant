"""Auth 模块。

职责（见 ``docs/modules.md`` 第 2 节与 ``docs/api-contract.md`` 第 2 节）：
注册、登录、刷新会话、注销、当前用户资料与平台角色守卫。

不负责：课程成员权限、课程资源所有权——这些由对应业务模块的
``permissions.py`` 结合本模块提供的当前用户依赖实现。
"""

__all__: list[str] = []
