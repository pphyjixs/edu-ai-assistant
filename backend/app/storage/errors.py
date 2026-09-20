"""对象存储错误类型与对外映射。

分层约定（对应 ``docs/api-contract.md`` §4.8 的错误表）：

- :class:`StorageObjectNotFoundError` 是**业务语义**：对象不存在（未直传或传到了别的键）。
  它不直接决定 HTTP 状态码——初始化与完成上传把它映射为
  ``422 UPLOAD_INVALID``（``details.reason = OBJECT_MISSING``），
  过期清理则把它当作"已经干净"而忽略。
- :class:`StorageUnavailableError` 是**基础设施故障**：超时、连接失败、5xx、
  凭据被拒、配置缺失。它统一映射为 ``503 SERVICE_UNAVAILABLE``
  （``details.component = storage``），绝不对外暴露 endpoint、凭据或响应正文。

对外错误由 :func:`as_service_unavailable` 统一构造，调用方只需一行
``raise as_service_unavailable(exc) from exc``，避免每处各写一份 details。
"""

from __future__ import annotations

from app.core.errors import ServiceUnavailableError

#: 错误响应 details 中的组件名（契约 4.8）
STORAGE_COMPONENT = "storage"


class StorageError(Exception):
    """存储层错误基类，便于调用方统一兜底。"""


class StorageObjectNotFoundError(StorageError):
    """对象不存在。"""

    def __init__(self, object_key: str) -> None:
        super().__init__(f"对象存储中不存在该对象（key={object_key}）")
        self.object_key = object_key


class StorageUnavailableError(StorageError):
    """对象存储暂时不可用或未正确配置。

    :param reason: 稳定的机器可读原因（``not_configured`` / ``timeout`` /
        ``connection`` / ``server_error`` / ``access_denied`` / ``unexpected_status``），
        只用于日志与排障，不包含任何凭据或响应正文。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def as_service_unavailable(exc: StorageUnavailableError) -> ServiceUnavailableError:
    """把存储故障转换为统一的对外 503 错误。

    消息固定为面向用户的通用文案；``details`` 只暴露组件名与原因码。
    """
    return ServiceUnavailableError(
        "对象存储暂时不可用，请稍后重试",
        details={"component": STORAGE_COMPONENT, "reason": exc.reason},
    )
