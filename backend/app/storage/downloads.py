"""预签名下载地址的签发（资料、实验报告、作业附件共用）。

三处业务都需要「给成员一个短时有效的原文下载地址」，实现完全一致：

1. 用对象键签一个 ``GET``；
2. 对象存储未配置或不可达时统一转成 ``503 SERVICE_UNAVAILABLE``
   （契约 4.x / 9.12 的错误约定），而不是把存储层的异常泄漏给调用方。

集中在这里是为了避免每个模块各写一份 try/except，并保证三处的
有效期来源与错误语义完全一致。
"""

from __future__ import annotations

from app.storage.errors import StorageUnavailableError, as_service_unavailable
from app.storage.s3 import PresignedDownload, S3Storage


def presign_download(
    storage: S3Storage, object_key: str, *, ttl_seconds: int
) -> PresignedDownload:
    """签发临时下载地址。

    :param ttl_seconds: 有效期（秒）。调用方传配置里的同一个值，
        过期后前端重新请求一次即可，服务端不缓存地址。
    :raises ServiceUnavailableError: 对象存储未配置或不可达（HTTP 503）。
    """
    try:
        return storage.create_presigned_get(object_key, expires_in=ttl_seconds)
    except StorageUnavailableError as exc:
        raise as_service_unavailable(exc) from exc


__all__ = ["presign_download"]
