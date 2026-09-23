"""上传协议的共享工具（课件、实验报告与作业附件共用）。

三处上传走的是同一套协议（``docs/api-contract.md`` §4）：

1. 后端签发预签名 ``PUT``，浏览器直传对象存储，文件体不经过 API 进程；
2. 完成确认时后端用 ``HeadObject`` 核对对象**存在、大小、类型与存储侧摘要**；
3. 任一核对失败都是 ``422 UPLOAD_INVALID``，并且**必须带稳定的
   ``details.reason``** —— 前端与测试都按键分支，``message`` 只给人看。

这个模块只放"三处必须完全一致"的部分：错误构造与对象核对。集中在这里是因为
原因码、message 与 details 键一旦分叉，前端就得为同一件事写三套判断。
"""

from __future__ import annotations

import logging

from app.core.errors import UploadInvalidError
from app.storage import (
    S3Storage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    as_service_unavailable,
)

logger = logging.getLogger("app.uploads")


def upload_invalid(
    reason: str,
    message: str,
    *,
    field: str | None = None,
    **extra: object,
) -> UploadInvalidError:
    """构造带稳定原因码的 ``UPLOAD_INVALID`` 错误（契约 4.8 / 9.2 / 8.15）。"""
    details: dict[str, object] = {"reason": reason}
    if field is not None:
        details["field"] = field
    details.update(extra)
    return UploadInvalidError(message, details=details)


def verify_stored_object(
    storage: S3Storage,
    *,
    object_key: str,
    size: int,
    content_type: str,
    sha256: str,
) -> None:
    """对象确认：核对存在性、大小、内容类型与存储侧摘要（契约 4.5）。

    只读取对象元数据，**不下载文件内容**；**不使用 ETag 代替 SHA-256**。
    存储侧未返回校验值时只记日志（部分 S3 兼容实现不返回该头），不因此拒绝上传。

    :raises UploadInvalidError: 对象缺失或与初始化声明不一致（422）。
    :raises ServiceUnavailableError: 对象存储不可达（503）。
    """
    try:
        stored = storage.head_object(object_key)
    except StorageObjectNotFoundError as exc:
        raise upload_invalid(
            "OBJECT_MISSING",
            "对象存储中未找到已上传的文件，请先按预签名地址上传",
        ) from exc
    except StorageUnavailableError as exc:
        raise as_service_unavailable(exc) from exc

    if stored.size != size:
        raise upload_invalid(
            "OBJECT_SIZE_MISMATCH",
            "已上传对象的大小与初始化声明不一致",
            declared_size=size,
            actual_size=stored.size,
        )

    if stored.content_type and stored.content_type != content_type:
        raise upload_invalid(
            "OBJECT_TYPE_MISMATCH",
            "已上传对象的类型与初始化声明不一致",
            declared_content_type=content_type,
            actual_content_type=stored.content_type,
        )

    stored_sha256 = stored.checksum_sha256_hex()
    if stored_sha256 is None:
        logger.warning(
            "对象存储未返回 x-amz-checksum-sha256，跳过内容摘要比对（key=%s）",
            object_key,
        )
    elif stored_sha256 != sha256:
        raise upload_invalid(
            "CHECKSUM_MISMATCH",
            "已上传对象的 SHA-256 与初始化声明不一致",
            declared_sha256=sha256,
            actual_sha256=stored_sha256,
        )


__all__ = ["upload_invalid", "verify_stored_object"]
