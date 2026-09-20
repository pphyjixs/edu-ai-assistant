"""内存版对象存储假实现（不匹配 ``test_*.py``，不会被 pytest 收集）。

为什么需要它：真实对象存储无法稳定地制造「对象缺失」「元数据被篡改」
「存储不可用」这些分支。真实适配器的 SigV4 与 HTTP 行为已由
``tests/unit/test_storage_signing.py`` 与 ``tests/integration/test_storage_minio.py``
覆盖；这里只替换**边界**，用来驱动 Materials 模块的业务分支。

继承 :class:`~app.storage.s3.S3Storage` 是为了保证方法签名与真实适配器一致：
一旦真实适配器改了签名，这里会立刻暴露出来。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from app.core.time import utc_now
from app.storage.errors import StorageObjectNotFoundError, StorageUnavailableError
from app.storage.s3 import (
    CONTENT_TYPE_HEADER,
    CHECKSUM_SHA256_HEADER,
    IF_NONE_MATCH_ANY,
    IF_NONE_MATCH_HEADER,
    PresignedUpload,
    S3Storage,
    S3StorageConfig,
    StoredObject,
    sha256_base64,
)


@dataclass
class FakeObject:
    """假对象存储里的一份对象。"""

    size: int
    content_type: str
    checksum_sha256_base64: str | None
    #: 对象内容（解析 Worker 测试用；生产适配器从真实存储拉取）
    content: bytes = b""


class FakeStorage(S3Storage):
    """记录预签名结果与对象元数据，不发起任何网络请求。"""

    def __init__(self) -> None:
        super().__init__(
            S3StorageConfig(
                endpoint="http://fake-storage.invalid",
                bucket="fake-bucket",
                access_key="fake-access-key",
                secret_key="fake-secret-key",
            )
        )
        #: 对象键 → 已上传对象
        self.objects: dict[str, FakeObject] = {}
        #: 对象键 → 最近一次签发的地址（测试用来断言"地址可用/不可用"）
        self.presigned_urls: dict[str, str] = {}
        #: 非空时所有操作都抛"存储不可用"，用于验证 503 分支
        self.unavailable_reason: str | None = None
        #: 设为 True 时 HeadObject 不返回校验值（模拟不实现该头的服务端）
        self.hide_checksum = False
        #: 设为 True 时模拟「晚到 PUT」：删除成功后对象立即被重建
        #: （契约 5.2：维护命令必须核查并继续清理）
        self.recreate_after_delete = False

    # ---------------------------- 测试辅助 ---------------------------- #
    def store_object(
        self,
        object_key: str,
        *,
        size: int,
        content_type: str,
        sha256_hex: str | None,
        content: bytes = b"",
    ) -> None:
        """放入一份对象；``sha256_hex`` 为 ``None`` 时不带校验值。"""
        self.objects[object_key] = FakeObject(
            size=size,
            content_type=content_type,
            checksum_sha256_base64=(
                sha256_base64(sha256_hex) if sha256_hex is not None else None
            ),
            content=content,
        )

    def as_unavailable(self, reason: str = "connection") -> None:
        self.unavailable_reason = reason

    def as_available(self) -> None:
        self.unavailable_reason = None

    def _guard(self) -> None:
        if self.unavailable_reason is not None:
            raise StorageUnavailableError("对象存储暂时不可用", reason=self.unavailable_reason)

    # ------------------------- 覆盖适配器行为 ------------------------- #
    def create_presigned_put(
        self,
        object_key: str,
        *,
        content_type: str,
        sha256_hex: str,
        expires_in: int | None = None,
        now=None,
    ) -> PresignedUpload:
        self._guard()
        issued_at = now or utc_now()
        ttl = expires_in or self.config.upload_url_ttl_seconds
        url = f"{self.config.endpoint}/{self.config.bucket}/{object_key}"
        self.presigned_urls[object_key] = url
        return PresignedUpload(
            url=url,
            method="PUT",
            headers={
                CONTENT_TYPE_HEADER: content_type,
                IF_NONE_MATCH_HEADER: IF_NONE_MATCH_ANY,
                CHECKSUM_SHA256_HEADER: sha256_base64(sha256_hex),
            },
            expires_at=issued_at + timedelta(seconds=ttl),
            content_type=content_type,
            checksum_sha256_base64=sha256_base64(sha256_hex),
        )

    def head_object(self, object_key: str) -> StoredObject:
        self._guard()
        stored = self.objects.get(object_key)
        if stored is None:
            raise StorageObjectNotFoundError(object_key)
        return StoredObject(
            key=object_key,
            size=stored.size,
            content_type=stored.content_type,
            checksum_sha256_base64=(
                None if self.hide_checksum else stored.checksum_sha256_base64
            ),
            # 刻意给一个与内容无关的值：任何用 ETag 冒充 SHA-256 的实现都会测出来
            etag='"fake-etag-not-a-content-digest"',
        )

    def delete_object(self, object_key: str) -> bool:
        self._guard()
        removed = self.objects.pop(object_key, None)
        if removed is not None and self.recreate_after_delete:
            # 模拟晚到 PUT 在 DELETE 之后到达并重建对象
            self.objects[object_key] = removed
            return True
        return removed is not None

    def get_object(self, object_key: str) -> bytes:
        """返回对象内容（与真实适配器的解析 Worker 读取路径一致）。"""
        self._guard()
        stored = self.objects.get(object_key)
        if stored is None:
            raise StorageObjectNotFoundError(object_key)
        return stored.content

    def get_object_verified(
        self,
        object_key: str,
        *,
        expected_size: int,
        expected_sha256_hex: str,
    ) -> bytes:
        """返回对象内容并复核大小与 SHA-256（与真实适配器语义一致）。"""
        from app.storage.errors import StorageVerificationError

        self._guard()
        stored = self.objects.get(object_key)
        if stored is None:
            raise StorageObjectNotFoundError(object_key)
        if stored.size != expected_size:
            raise StorageVerificationError(
                f"对象实际大小（{stored.size}）与资料声明（{expected_size}）不一致",
                reason="size_mismatch",
            )
        actual = hashlib.sha256(stored.content).hexdigest()
        if actual != expected_sha256_hex.lower():
            raise StorageVerificationError(
                "对象内容的 SHA-256 与资料声明不一致，已拒绝解析",
                reason="checksum_mismatch",
            )
        return stored.content

    def purged(self) -> bool:
        """测试断言用：后端从未接收过文件内容，因此也不该缓存对象。"""
        return not self.objects
