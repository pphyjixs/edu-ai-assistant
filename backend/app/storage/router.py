"""本地持久化文件的签名上传与下载端点。

这两个端点只在 ``STORAGE_BACKEND=local`` 时可用。业务接口签发带
HMAC 的短期 URL，浏览器仍使用现有的三步上传流程。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from app.storage.deps import StorageDep
from app.storage.errors import StorageObjectNotFoundError, StorageUnavailableError
from app.storage.s3 import (
    CHECKSUM_SHA256_HEADER,
    CONTENT_TYPE_HEADER,
    IF_NONE_MATCH_ANY,
    IF_NONE_MATCH_HEADER,
    StorageConfigError,
)

storage_router = APIRouter(prefix="/storage", tags=["storage"])


def _ensure_local(storage: StorageDep) -> None:
    if not storage.is_local:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@storage_router.put("/objects/{token}", include_in_schema=False)
async def put_local_object(
    token: str,
    request: Request,
    storage: StorageDep,
) -> Response:
    _ensure_local(storage)
    try:
        payload = storage.verify_local_token(token, method="PUT")
    except StorageConfigError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    content_type = request.headers.get(CONTENT_TYPE_HEADER, "")
    checksum = request.headers.get(CHECKSUM_SHA256_HEADER, "")
    if (
        content_type != payload.get("content_type")
        or checksum != payload.get("checksum")
        or request.headers.get(IF_NONE_MATCH_HEADER) != IF_NONE_MATCH_ANY
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="上传请求头与签名不一致",
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > storage.config.local_max_upload_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="文件超过上传大小限制",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Content-Length 无效",
            ) from exc

    body = await request.body()
    try:
        storage.put_local_object(
            str(payload["key"]),
            body,
            content_type=content_type,
            checksum_sha256_base64=checksum,
        )
    except FileExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="对象已存在",
        ) from exc
    except StorageConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="本地文件存储暂时不可用",
        ) from exc
    return Response(status_code=status.HTTP_200_OK)


@storage_router.get("/objects/{token}", include_in_schema=False)
async def get_local_object(token: str, storage: StorageDep) -> FileResponse:
    _ensure_local(storage)
    try:
        path, content_type = storage.get_local_download(token)
    except StorageConfigError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except StorageObjectNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="本地文件存储暂时不可用",
        ) from exc
    return FileResponse(path, media_type=content_type)


__all__ = ["storage_router"]
