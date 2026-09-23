"""``/api/v1`` 路由聚合。

各业务模块在自己的 ``app/modules/<module>/router.py`` 中提供 ``APIRouter``，
统一在这里聚合，再由 ``app.main`` 挂载到 ``Settings.api_v1_prefix`` 上。
这样接口版本切换、全局依赖（认证、限流）只需要改这一处。

第一版计划的模块与路径前缀见 ``docs/api-contract.md``：

==============  ==================
模块            前缀
==============  ==================
auth            ``/auth``、``/users/me``
courses         ``/courses``
materials       ``/courses/{id}/materials``、``/materials``
chat            ``/courses/{id}/chat-sessions``、``/chat-sessions``
learning        ``/practice-sets``
assignments     ``/assignments``
grading         ``/submissions``、``/grade-reviews``
jobs            ``/jobs``
dashboard       ``/dashboard``
==============  ==================

当前已接入：``auth``（含 ``/users/me``）、``courses``、``materials``、
``chat``（课程问答，契约第 6 节）、``practice``（课程练习，契约第 7 节）、
``assignments``（实验任务，契约第 8 节）、``grading``（提交与批改，契约第 9 节）、
``jobs``。其余模块由各自负责人实现后在此登记。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.agent.router import agent_router
from app.modules.assignments.router import assignments_router
from app.modules.auth.router import auth_router, me_router
from app.modules.chat.router import chat_router
from app.modules.courses.router import courses_router
from app.modules.grading.router import grading_router
from app.modules.jobs.router import jobs_router
from app.modules.materials.router import materials_router
from app.modules.practice.router import practice_router

#: v1 路由聚合器；业务模块实现后在此 include_router。
api_router = APIRouter()

# ------------------------------- auth -------------------------------
api_router.include_router(auth_router)
api_router.include_router(me_router)

# ------------------------------ courses ------------------------------
api_router.include_router(courses_router)

# ----------------------------- materials -----------------------------
api_router.include_router(materials_router)

# -------------------------------- chat --------------------------------
api_router.include_router(chat_router)

# ------------------------------ practice ------------------------------
api_router.include_router(practice_router)

# ----------------------------- assignments ----------------------------
api_router.include_router(assignments_router)

# ------------------------------ agent ----------------------------------
api_router.include_router(agent_router)

# ------------------------------- grading -------------------------------
api_router.include_router(grading_router)

# -------------------------------- jobs -------------------------------
api_router.include_router(jobs_router)

__all__ = ["api_router"]
