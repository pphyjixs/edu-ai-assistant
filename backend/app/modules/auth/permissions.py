"""Auth 权限依赖：当前用户与平台角色守卫。

对外提供三个注入点：

- :data:`CurrentUserDep`：解析 ``Authorization: Bearer <access_token>``，
  校验签名与到期时间，并从数据库加载用户；
- :data:`TeacherDep` / :data:`StudentDep`：平台角色守卫。

平台角色只是默认权限。课程成员、资源所有者等资源级判断由对应业务模块的
``permissions.py`` 在拿到 :data:`CurrentUserDep` 之后自行完成
（见 ``docs/modules.md`` 第 1 节）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import SettingsDep
from app.core.errors import RoleForbiddenError, TokenExpiredError
from app.db.session import SessionDep
from app.modules.auth import repository as repo
from app.modules.auth.models import User, UserRole
from app.modules.auth.security import decode_access_token

#: auto_error=False：Authorization 头缺失或格式错误时由本模块抛出契约错误码，
#: 而不是 FastAPI 默认的英文 403。
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")

_BearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
]

RoleGuard = Callable[[User], Awaitable[User]]


async def get_current_user(
    request: Request,
    credentials: _BearerCredentials,
    session: SessionDep,
    settings: SettingsDep,
) -> User:
    """从 Bearer 令牌解析当前用户。

    注销不维护 Access Token 黑名单，因此这里唯一的时效性检查是令牌自身的过期时间；
    但每次仍会回查数据库，保证被停用的账号立刻失去访问能力。
    """
    if credentials is None or not credentials.credentials.strip():
        raise TokenExpiredError("缺少访问令牌，请先登录")

    claims = decode_access_token(credentials.credentials, settings)
    user = await repo.get_user_by_id(session, claims.user_id)
    if user is None or not user.is_active:
        raise TokenExpiredError()

    # 供访问日志记录用户 ID（见 app.core.middleware）
    request.state.user_id = str(user.id)
    return user


#: 注入当前用户
CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> RoleGuard:
    """生成平台角色守卫依赖。

    用法::

        @router.post("/courses", dependencies=[Depends(require_roles(UserRole.TEACHER))])
    """
    allowed = frozenset(roles)

    async def _guard(user: CurrentUserDep) -> User:
        if user.role not in allowed:
            raise RoleForbiddenError()
        return user

    return _guard


#: 仅教师
TeacherDep = Annotated[User, Depends(require_roles(UserRole.TEACHER))]

#: 仅学生
StudentDep = Annotated[User, Depends(require_roles(UserRole.STUDENT))]
