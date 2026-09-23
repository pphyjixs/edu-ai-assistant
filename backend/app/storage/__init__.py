"""对象存储接入层：S3 兼容适配器、对象键规则与错误映射。

对外只暴露四样东西：

- :class:`S3Storage`：预签名 PUT、HeadObject 对象确认、删除对象；
- :func:`get_storage` / :func:`close_storages`：进程内共享实例与生命周期；
- :func:`build_upload_object_key`：后端生成对象键（不含用户文件名）；
- 错误类型与 :func:`as_service_unavailable`：统一映射为 503 ``SERVICE_UNAVAILABLE``。

**适配器不接收也不保存文件内容**：文件体由浏览器按预签名地址直传对象存储
（见 ``docs/api-contract.md`` §4.4）。
"""

from __future__ import annotations

from app.storage.errors import (
    StorageError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    StorageVerificationError,
    as_service_unavailable,
)
from app.storage.keys import (
    InvalidExtensionError,
    build_submission_object_key,
    build_upload_object_key,
    normalize_extension,
)
from app.storage.s3 import (
    S3Storage,
    S3StorageConfig,
    PresignedDownload,
    PresignedUpload,
    StorageConfigError,
    StoredObject,
    close_storages,
    get_storage,
    sha256_base64,
)

__all__ = [
    "InvalidExtensionError",
    "PresignedDownload",
    "PresignedUpload",
    "S3Storage",
    "S3StorageConfig",
    "StorageConfigError",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageUnavailableError",
    "StorageVerificationError",
    "StoredObject",
    "as_service_unavailable",
    "build_submission_object_key",
    "build_upload_object_key",
    "close_storages",
    "get_storage",
    "normalize_extension",
    "sha256_base64",
]
