"""S3 兼容对象存储适配器（SigV4 预签名直传）。

对应 ``docs/api-contract.md`` §4.3 / §4.4 / §4.5 与 plan 第 3 步。

职责边界（重要）：

- 本模块**只签名、只读元数据**，从不接收或保存文件内容。浏览器拿到预签名地址后
  把字节流直接 PUT 给对象存储，后端进程全程不经手文件体。
- 预签名 PUT 由 :meth:`S3Storage.create_presigned_put` 用 SigV4 在本地计算，
  不产生任何网络请求。
- 对象确认由 :meth:`S3Storage.head_object` 执行 HeadObject，只返回大小、类型与
  存储侧校验值。**不使用 ETag 代替 SHA-256**：ETag 对分片上传不是内容摘要，
  单对象校验必须以 ``x-amz-checksum-sha256``（Base64）为准。

预签名 PUT 强制携带三个头（契约 §4.4，客户端必须逐字带回）：

===========================  ====================================================
``Content-Type``             声明的规范 MIME，随签名一起校验
``x-amz-checksum-sha256``    完整对象的 SHA-256（Base64）；内容与声明不符时存储侧拒绝
``If-None-Match: *``         条件写入：对象已存在时返回 412，重复 PUT 被拒绝
===========================  ====================================================

故障处理：连接超时、连接失败、5xx、凭据被拒、配置缺失统一抛出
:class:`~app.storage.errors.StorageUnavailableError`，调用方用
:func:`~app.storage.errors.as_service_unavailable` 一行转成
``503 SERVICE_UNAVAILABLE``。所有 HTTP 调用都带显式超时。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

import httpx

from app.core.config import Settings
from app.core.time import utc_now
from app.storage.errors import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
)

logger = logging.getLogger("app.storage")

#: SigV4 固定算法标识
ALGORITHM = "AWS4-HMAC-SHA256"

#: S3 服务在签名范围中的服务名
SERVICE = "s3"

#: 预签名 PUT 使用未签名载荷：后端不读取文件体，无法预先计算请求体摘要
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

#: 空载荷摘要（HEAD 等无请求体的签名请求使用）
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()

#: 条件写入头（契约 §4.4）：对象已存在时拒绝覆盖
IF_NONE_MATCH_HEADER = "If-None-Match"
IF_NONE_MATCH_ANY = "*"

#: 完整对象校验头（Base64）：存储侧据此校验内容与声明是否一致
CHECKSUM_SHA256_HEADER = "x-amz-checksum-sha256"

#: 内容类型头
CONTENT_TYPE_HEADER = "Content-Type"

#: JSON 之外的默认内容类型（HEAD 缺省时使用）
DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: 需要按字符串编码规则转义的字符之外的保留集（SigV4 要求编码除 unreserved 外全部字符）
_UNRESERVED = "-_.~"

#: 判定为"暂时性故障"的 HTTP 状态码
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})


class StorageConfigError(ValueError):
    """调用参数不符合存储侧要求（例如 sha256 不是 64 位十六进制）。"""


@dataclass(frozen=True)
class S3StorageConfig:
    """对象存储连接配置（从 :class:`Settings` 派生，便于单测直接构造）。"""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    path_style: bool = True
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 10.0
    upload_url_ttl_seconds: int = 10 * 60

    @classmethod
    def from_settings(cls, settings: Settings) -> S3StorageConfig:
        """从应用配置派生存储配置。"""
        return cls(
            endpoint=settings.storage_endpoint.strip().rstrip("/"),
            bucket=settings.storage_bucket.strip(),
            access_key=settings.storage_access_key.strip(),
            secret_key=settings.storage_secret_key.strip(),
            region=settings.storage_region.strip() or "us-east-1",
            path_style=settings.storage_path_style,
            connect_timeout_seconds=settings.storage_connect_timeout_seconds,
            read_timeout_seconds=settings.storage_read_timeout_seconds,
            upload_url_ttl_seconds=settings.storage_upload_url_ttl_seconds,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)

    def problem(self) -> str | None:
        """返回面向运维的配置缺失说明；配置齐备时返回 ``None``。

        只报缺失的**变量名**，不回显任何取值。
        """
        missing = [
            name
            for name, value in (
                ("STORAGE_ENDPOINT", self.endpoint),
                ("STORAGE_BUCKET", self.bucket),
                ("STORAGE_ACCESS_KEY", self.access_key),
                ("STORAGE_SECRET_KEY", self.secret_key),
            )
            if not value
        ]
        if missing:
            return "对象存储未配置：" + "、".join(missing)
        return None


@dataclass(frozen=True)
class PresignedUpload:
    """初始化上传的响应数据（契约 §4.3）。"""

    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime
    content_type: str
    checksum_sha256_base64: str


@dataclass(frozen=True)
class PresignedDownload:
    """预签名 GET 的响应数据（契约 §9.5 提交详情）。"""

    url: str
    method: str
    expires_at: datetime


@dataclass(frozen=True)
class StoredObject:
    """HeadObject 读到的对象元数据（契约 §4.5 对象确认）。"""

    key: str
    size: int
    content_type: str
    #: 存储侧记录的完整对象 SHA-256（Base64）；未记录时为 None
    checksum_sha256_base64: str | None
    #: 仅用于日志排障；**绝不**当作内容摘要参与校验
    etag: str | None

    def checksum_sha256_hex(self) -> str | None:
        """把存储侧校验值转成小写十六进制，便于与声明值比较。"""
        if not self.checksum_sha256_base64:
            return None
        try:
            raw = base64.b64decode(self.checksum_sha256_base64, validate=True)
        except (binascii.Error, ValueError):
            return None
        return raw.hex()


def sha256_base64(sha256_hex: str) -> str:
    """把 64 位十六进制摘要转成 Base64（``x-amz-checksum-sha256`` 的取值格式）。

    :raises StorageConfigError: 输入不是 64 位十六进制字符串。
    """
    value = (sha256_hex or "").strip().lower()
    if len(value) != 64:
        raise StorageConfigError("sha256 必须是 64 位十六进制字符串")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise StorageConfigError("sha256 必须是 64 位十六进制字符串") from exc
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- #
# SigV4 签名
# --------------------------------------------------------------------------- #
def _quote(value: str, *, safe: str = _UNRESERVED) -> str:
    return quote(value, safe=safe)


def signing_key(
    secret_key: str, date_stamp: str, region: str, service: str = SERVICE
) -> bytes:
    """派生 SigV4 签名密钥（AWS 文档中的 kSigning）。

    对应 AWS 官方示例（20150830 / us-east-1 / iam）的已知结果，
    单测用该向量做已知答案校验。
    """
    date_key = hmac.new(
        f"AWS4{secret_key}".encode(), date_stamp.encode(), hashlib.sha256
    ).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        f"{_quote(key)}={_quote(value)}" for key, value in sorted(params.items())
    )


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    """规范化请求头，返回（canonical_headers 文本, signed_headers 列表）。"""
    items = sorted(
        (name.lower().strip(), " ".join(value.strip().split()))
        for name, value in headers.items()
    )
    rendered = "".join(f"{name}:{value}\n" for name, value in items)
    signed = ";".join(name for name, _ in items)
    return rendered, signed


def _canonical_uri(path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return _quote(path, safe="/")


def _string_to_sign(
    amz_date: str, scope: str, canonical_request: str
) -> str:
    digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    return f"{ALGORITHM}\n{amz_date}\n{scope}\n{digest}"


def _sign(
    secret_key: str, date_stamp: str, region: str, string_to_sign: str
) -> str:
    key = signing_key(secret_key, date_stamp, region)
    return hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class S3Storage:
    """S3 兼容对象存储适配器。

    :param config: 存储配置
    :param client: 可选的 ``httpx.Client``（测试注入 ``MockTransport`` 用）；
        未提供时按配置懒加载一个带显式超时的客户端。
    """

    def __init__(
        self,
        config: S3StorageConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client

    # ------------------------------ 基础 ------------------------------ #
    @property
    def config(self) -> S3StorageConfig:
        return self._config

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    def _require_configured(self) -> None:
        problem = self._config.problem()
        if problem:
            raise StorageUnavailableError(problem, reason="not_configured")

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=self._config.connect_timeout_seconds,
                    read=self._config.read_timeout_seconds,
                    write=self._config.read_timeout_seconds,
                    pool=self._config.connect_timeout_seconds,
                ),
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        """关闭内部 HTTP 客户端（生命周期结束时调用）。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _object_url(self, object_key: str) -> str:
        """对象地址；path-style 把桶名放进路径，否则用 virtual-host 子域。"""
        key_path = _quote(object_key, safe="/")
        if self._config.path_style:
            bucket = _quote(self._config.bucket, safe=_UNRESERVED)
            return f"{self._config.endpoint}/{bucket}/{key_path}"
        parsed = urlparse(self._config.endpoint)
        scheme = parsed.scheme or "https"
        return f"{scheme}://{self._bucket_host()}/{key_path}"

    def _object_path(self, object_key: str) -> str:
        if self._config.path_style:
            return f"/{self._config.bucket}/{object_key}"
        return f"/{object_key}"

    def _bucket_host(self) -> str:
        """请求的 Host 值（virtual-host 时含桶名前缀），签名必须与实际一致。"""
        host = urlparse(self._config.endpoint).netloc
        if self._config.path_style:
            return host
        return f"{self._config.bucket}.{host}"


    # --------------------------- 预签名直传 ---------------------------- #
    def create_presigned_put(
        self,
        object_key: str,
        *,
        content_type: str,
        sha256_hex: str,
        expires_in: int | None = None,
        now: datetime | None = None,
    ) -> PresignedUpload:
        """签发 PUT 预签名地址，并返回客户端必须原样发送的头。

        纯本地计算，**不发起任何网络请求**，也不接触文件内容。

        :raises StorageUnavailableError: 存储未配置。
        :raises StorageConfigError: ``content_type`` 为空或 ``sha256`` 不是 64 位十六进制。
        """
        self._require_configured()

        mime = (content_type or "").strip()
        if not mime:
            raise StorageConfigError("content_type 不能为空")
        checksum_b64 = sha256_base64(sha256_hex)

        ttl = expires_in or self._config.upload_url_ttl_seconds
        issued_at = now or utc_now()
        expires_at = issued_at + timedelta(seconds=ttl)
        amz_date = issued_at.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = issued_at.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self._config.region}/{SERVICE}/aws4_request"

        # 客户端必须逐字带回的请求头；全部参与签名，任一缺失/被改都会导致签名不符
        request_headers = {
            CONTENT_TYPE_HEADER: mime,
            IF_NONE_MATCH_HEADER: IF_NONE_MATCH_ANY,
            CHECKSUM_SHA256_HEADER: checksum_b64,
        }
        signed_header_map = {"host": self._bucket_host(), **request_headers}
        canonical_headers, signed_headers = _canonical_headers(signed_header_map)

        query = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self._config.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(ttl),
            "X-Amz-SignedHeaders": signed_headers,
        }
        canonical_query = _canonical_query(query)
        canonical_request = "\n".join(
            (
                "PUT",
                _canonical_uri(self._object_path(object_key)),
                canonical_query,
                canonical_headers,
                signed_headers,
                UNSIGNED_PAYLOAD,
            )
        )
        signature = _sign(
            self._config.secret_key,
            date_stamp,
            self._config.region,
            _string_to_sign(amz_date, scope, canonical_request),
        )

        url = (
            f"{self._object_url(object_key)}"
            f"?{canonical_query}&X-Amz-Signature={signature}"
        )
        return PresignedUpload(
            url=url,
            method="PUT",
            headers=dict(request_headers),
            expires_at=expires_at,
            content_type=mime,
            checksum_sha256_base64=checksum_b64,
        )

    # --------------------------- 预签名下载 ---------------------------- #
    def create_presigned_get(
        self,
        object_key: str,
        *,
        expires_in: int | None = None,
        now: datetime | None = None,
    ) -> PresignedDownload:
        """签发 GET 预签名地址，供提交详情下发临时下载链接（契约 §9.5）。

        纯本地计算，不发起网络请求；只签名 ``host``，因此下载地址可被浏览器
        直接打开，不需要附加任何请求头。
        """
        self._require_configured()

        ttl = expires_in or self._config.upload_url_ttl_seconds
        issued_at = now or utc_now()
        expires_at = issued_at + timedelta(seconds=ttl)
        amz_date = issued_at.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = issued_at.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self._config.region}/{SERVICE}/aws4_request"

        canonical_headers, signed_headers = _canonical_headers(
            {"host": self._bucket_host()}
        )
        query = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self._config.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(ttl),
            "X-Amz-SignedHeaders": signed_headers,
        }
        canonical_query = _canonical_query(query)
        canonical_request = "\n".join(
            (
                "GET",
                _canonical_uri(self._object_path(object_key)),
                canonical_query,
                canonical_headers,
                signed_headers,
                UNSIGNED_PAYLOAD,
            )
        )
        signature = _sign(
            self._config.secret_key,
            date_stamp,
            self._config.region,
            _string_to_sign(amz_date, scope, canonical_request),
        )
        return PresignedDownload(
            url=(
                f"{self._object_url(object_key)}"
                f"?{canonical_query}&X-Amz-Signature={signature}"
            ),
            method="GET",
            expires_at=expires_at,
        )

    # ------------------------------ 建桶 ------------------------------ #
    def bucket_exists(self) -> bool:
        """探测桶是否存在（HEAD bucket）。"""
        self._require_configured()
        headers = self._signed_headers("HEAD", f"/{self._config.bucket}", {})
        url = f"{self._config.endpoint}/{_quote(self._config.bucket, safe=_UNRESERVED)}"
        try:
            response = self._http().request("HEAD", url, headers=headers)
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise StorageUnavailableError(
            f"对象存储返回非预期状态 {response.status_code}",
            reason="unexpected_status",
        )

    def ensure_bucket(self) -> bool:
        """确保桶存在，返回是否新建。

        仅用于本地开发与验收脚本的就地准备（线上桶由部署流程预先创建），
        不参与任何业务请求路径。
        """
        self._require_configured()
        if self.bucket_exists():
            return False

        headers = self._signed_headers("PUT", f"/{self._config.bucket}", {})
        url = f"{self._config.endpoint}/{_quote(self._config.bucket, safe=_UNRESERVED)}"
        try:
            response = self._http().request("PUT", url, headers=headers, content=b"")
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if response.status_code in (200, 204):
            logger.info("已创建对象存储桶（bucket=%s）", self._config.bucket)
            return True
        raise StorageUnavailableError(
            f"创建桶失败（status={response.status_code}）",
            reason="unexpected_status",
        )

    # ----------------------------- 对象确认 ---------------------------- #
    def head_object(self, object_key: str) -> StoredObject:
        """读取对象元数据（大小、内容类型、存储侧校验值）。

        :raises StorageObjectNotFoundError: 对象不存在（未直传或传到了别的键）。
        :raises StorageUnavailableError: 超时、连接失败、5xx、凭据被拒或未配置。
        """
        self._require_configured()
        headers = self._signed_headers(
            "HEAD", self._object_path(object_key), {"x-amz-checksum-mode": "ENABLED"}
        )
        url = self._object_url(object_key)

        try:
            response = self._http().request("HEAD", url, headers=headers)
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if response.status_code == 404:
            raise StorageObjectNotFoundError(object_key)
        if response.status_code in (401, 403):
            # 凭据错误属于部署配置问题，不能当作"对象不存在"或用户错误
            logger.warning("对象存储拒绝访问（status=%s）", response.status_code)
            raise StorageUnavailableError("对象存储拒绝了访问凭据", reason="access_denied")
        if response.status_code in _TRANSIENT_STATUS:
            raise StorageUnavailableError(
                f"对象存储返回 {response.status_code}", reason="server_error"
            )
        if response.status_code != 200:
            raise StorageUnavailableError(
                f"对象存储返回非预期状态 {response.status_code}",
                reason="unexpected_status",
            )

        return StoredObject(
            key=object_key,
            size=_int_header(response, "content-length"),
            content_type=response.headers.get("content-type", DEFAULT_CONTENT_TYPE),
            checksum_sha256_base64=response.headers.get(CHECKSUM_SHA256_HEADER),
            etag=response.headers.get("etag"),
        )

    # ------------------------------ 删除 ------------------------------ #
    def delete_object(self, object_key: str) -> bool:
        """删除对象，返回是否真的删除了（对象本就不存在时返回 ``False``）。

        供过期上传清理使用；同样不接收文件内容。
        """
        self._require_configured()
        headers = self._signed_headers("DELETE", self._object_path(object_key), {})
        url = self._object_url(object_key)

        try:
            response = self._http().request("DELETE", url, headers=headers)
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if response.status_code == 404:
            return False
        if response.status_code in (401, 403):
            logger.warning("对象存储拒绝访问（status=%s）", response.status_code)
            raise StorageUnavailableError("对象存储拒绝了访问凭据", reason="access_denied")
        if response.status_code in _TRANSIENT_STATUS:
            raise StorageUnavailableError(
                f"对象存储返回 {response.status_code}", reason="server_error"
            )
        if response.status_code not in (200, 204):
            raise StorageUnavailableError(
                f"对象存储返回非预期状态 {response.status_code}",
                reason="unexpected_status",
            )
        return True

    # ------------------------------ 读取 ------------------------------ #
    def get_object(self, object_key: str) -> bytes:
        """读取对象完整内容（解析 Worker 使用，契约 5.5）。

        与上传协议不同：这里由服务端主动拉取字节流，不涉及浏览器直传。

        :raises StorageObjectNotFoundError: 对象不存在。
        :raises StorageUnavailableError: 超时、连接失败、5xx、凭据被拒或未配置。
        """
        self._require_configured()
        headers = self._signed_headers("GET", self._object_path(object_key), {})
        url = self._object_url(object_key)

        try:
            response = self._http().request("GET", url, headers=headers)
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if response.status_code == 404:
            raise StorageObjectNotFoundError(object_key)
        if response.status_code in (401, 403):
            logger.warning("对象存储拒绝访问（status=%s）", response.status_code)
            raise StorageUnavailableError("对象存储拒绝了访问凭据", reason="access_denied")
        if response.status_code in _TRANSIENT_STATUS:
            raise StorageUnavailableError(
                f"对象存储返回 {response.status_code}", reason="server_error"
            )
        if response.status_code != 200:
            raise StorageUnavailableError(
                f"对象存储返回非预期状态 {response.status_code}",
                reason="unexpected_status",
            )
        return response.content

    def get_object_verified(
        self,
        object_key: str,
        *,
        expected_size: int,
        expected_sha256_hex: str,
    ) -> bytes:
        """流式下载对象并复核大小与 SHA-256（解析 Worker 专用，契约 5.5）。

        用 ``httpx`` 的流式响应分块读取：边读边累计字节数与内容摘要，
        与资料记录声明的 ``size`` / ``sha256`` 比对，不符立即拒绝——
        防止对象被其他途径改写后仍被解析。

        :raises StorageObjectNotFoundError: 对象不存在。
        :raises StorageVerificationError: 大小或 SHA-256 与声明不符。
        :raises StorageUnavailableError: 超时、连接失败、5xx、凭据被拒或未配置。
        """
        self._require_configured()
        headers = self._signed_headers("GET", self._object_path(object_key), {})
        url = self._object_url(object_key)

        hasher = hashlib.sha256()
        size_seen = 0
        parts: list[bytes] = []
        try:
            with self._http().stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    raise StorageObjectNotFoundError(object_key)
                if response.status_code in (401, 403):
                    logger.warning(
                        "对象存储拒绝访问（status=%s）", response.status_code
                    )
                    raise StorageUnavailableError(
                        "对象存储拒绝了访问凭据", reason="access_denied"
                    )
                if response.status_code in _TRANSIENT_STATUS:
                    raise StorageUnavailableError(
                        f"对象存储返回 {response.status_code}", reason="server_error"
                    )
                if response.status_code != 200:
                    raise StorageUnavailableError(
                        f"对象存储返回非预期状态 {response.status_code}",
                        reason="unexpected_status",
                    )
                for block in response.iter_bytes():
                    size_seen += len(block)
                    hasher.update(block)
                    parts.append(block)
        except httpx.TimeoutException as exc:
            raise StorageUnavailableError(
                f"对象存储请求超时（{type(exc).__name__}）", reason="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailableError(
                f"对象存储连接失败（{type(exc).__name__}）", reason="connection"
            ) from exc

        if size_seen != expected_size:
            raise StorageVerificationError(
                f"对象实际大小（{size_seen}）与资料声明（{expected_size}）不一致",
                reason="size_mismatch",
            )
        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != expected_sha256_hex.lower():
            raise StorageVerificationError(
                "对象内容的 SHA-256 与资料声明不一致，已拒绝解析",
                reason="checksum_mismatch",
            )
        return b"".join(parts)

    # ---------------------------- 请求签名 ---------------------------- #
    def _signed_headers(
        self, method: str, path: str, extra_headers: dict[str, str]
    ) -> dict[str, str]:
        """为普通（非预签名）请求生成 Authorization 头。"""
        now = utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self._config.region}/{SERVICE}/aws4_request"

        headers = {
            "host": self._bucket_host(),
            "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256,
            "x-amz-date": amz_date,
            **extra_headers,
        }
        canonical_headers, signed_headers = _canonical_headers(headers)
        canonical_request = "\n".join(
            (
                method,
                _canonical_uri(path),
                "",
                canonical_headers,
                signed_headers,
                EMPTY_PAYLOAD_SHA256,
            )
        )
        signature = _sign(
            self._config.secret_key,
            date_stamp,
            self._config.region,
            _string_to_sign(amz_date, scope, canonical_request),
        )
        return {
            **extra_headers,
            "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256,
            "x-amz-date": amz_date,
            "Authorization": (
                f"{ALGORITHM} Credential={self._config.access_key}/{scope},"
                f" SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }


def _int_header(response: httpx.Response, name: str) -> int:
    raw = response.headers.get(name)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# 进程内共享实例
# --------------------------------------------------------------------------- #
_storages: dict[tuple[object, ...], S3Storage] = {}


def _cache_key(config: S3StorageConfig) -> tuple[object, ...]:
    """按连接配置缓存适配器；不同配置（含测试库/测试桶）互不干扰。"""
    return (
        config.endpoint,
        config.bucket,
        config.access_key,
        config.secret_key,
        config.region,
        config.path_style,
        config.connect_timeout_seconds,
        config.read_timeout_seconds,
        config.upload_url_ttl_seconds,
    )


def get_storage(settings: Settings) -> S3Storage:
    """返回（并按需创建）配置对应的存储适配器。"""
    config = S3StorageConfig.from_settings(settings)
    key = _cache_key(config)
    storage = _storages.get(key)
    if storage is None:
        storage = S3Storage(config)
        _storages[key] = storage
    return storage


def close_storages() -> None:
    """关闭并清理全部适配器与其 HTTP 客户端，用于应用关闭钩子。"""
    for storage in list(_storages.values()):
        storage.close()
    _storages.clear()
