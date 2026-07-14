from datetime import datetime, timezone
from threading import local
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from pymongo import UpdateOne
from django.conf import settings

from .auth import CurrentUser
from .mongo import media_collection, media_user_state_collection

_SESSION_STATE = local()


def sync_from_webhard(current_user: CurrentUser, limit: int | None = None) -> dict[str, Any]:
    rows = fetch_webhard_media(current_user, limit or settings.MEDIA_CONFIG["MEDIA_SYNC_LIMIT"])
    collection = media_collection()
    now = datetime.now(timezone.utc)
    operations = []
    public_file_ids = []
    for row in rows:
        should_publish = current_user.is_admin and str(row.get("owner_user_id") or "") == current_user.user_id
        if should_publish and str(row.get("media_public_yn") or "") != "Y":
            public_file_ids.append(int(row["file_id"]))
        doc = media_document(row, now, owner_is_admin=should_publish or str(row.get("media_public_yn") or "") == "Y")
        operations.append(
            UpdateOne(
                {"webhard_file_id": doc["webhard_file_id"]},
                {
                    "$set": doc,
                    "$setOnInsert": {
                        "tags": [],
                        "album": "",
                        "favorite": False,
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        )
    if public_file_ids:
        mark_media_public(current_user, public_file_ids)
    deleted_count = purge_deleted_webhard_media(current_user)
    if not operations:
        return {"scanned_count": 0, "upserted_count": 0, "deleted_count": deleted_count}
    result = collection.bulk_write(operations, ordered=False)
    return {"scanned_count": len(rows), "upserted_count": result.upserted_count + result.modified_count, "deleted_count": deleted_count}


def sync_one_from_webhard(current_user: CurrentUser, file_id: int) -> dict[str, Any]:
    rows = fetch_webhard_media_by_file_id(current_user, file_id)
    collection = media_collection()
    now = datetime.now(timezone.utc)
    if not rows:
        query = {"webhard_file_id": file_id}
        if not current_user.is_admin:
            query["owner_user_id"] = current_user.user_id
        deleted_count = collection.delete_many(query).deleted_count
        if deleted_count:
            media_user_state_collection().delete_many({"webhard_file_id": file_id})
        return {"item": None, "deleted_count": deleted_count}
    for row in rows:
        should_publish = current_user.is_admin and str(row.get("owner_user_id") or "") == current_user.user_id
        if should_publish and str(row.get("media_public_yn") or "") != "Y":
            mark_media_public(current_user, [file_id])
        doc = media_document(row, now, owner_is_admin=should_publish or str(row.get("media_public_yn") or "") == "Y")
        collection.update_one(
            {"webhard_file_id": doc["webhard_file_id"]},
            {
                "$set": doc,
                "$setOnInsert": {
                    "tags": [],
                    "album": "",
                    "favorite": False,
                    "created_at": now,
                },
            },
            upsert=True,
        )
    return {"item": media_collection().find_one({"webhard_file_id": file_id})}


def fetch_webhard_media(current_user: CurrentUser, limit: int) -> list[dict[str, Any]]:
    data = internal_post(
        current_user,
        "/internal/media/list.json",
        {
            "viewer_user_id": current_user.user_id,
            "viewer_is_admin": current_user.is_admin,
            "limit": limit,
        },
    )
    items = data.get("items") or []
    return items if isinstance(items, list) else []


def fetch_webhard_media_by_file_id(current_user: CurrentUser, file_id: int) -> list[dict[str, Any]]:
    file = fetch_webhard_file(current_user, file_id, allow_public=False)
    if not file:
        return []
    return [file]


def fetch_webhard_file(current_user: CurrentUser, file_id: int, allow_public: bool = False) -> dict[str, Any] | None:
    data = internal_post(
        current_user,
        "/internal/media/file-detail.json",
        {
            "file_id": file_id,
            "viewer_user_id": current_user.user_id,
            "viewer_is_admin": current_user.is_admin,
            "allow_public": allow_public,
        },
    )
    item = data.get("item")
    return item if isinstance(item, dict) else None


def stream_webhard_file(current_user: CurrentUser, file_id: int, file_kind: str, allow_public: bool = False, range_header: str = "", quality: str = "") -> requests.Response:
    payload: dict[str, Any] = {
        "file_id": file_id,
        "file_kind": file_kind,
        "viewer_user_id": current_user.user_id,
        "viewer_is_admin": current_user.is_admin,
        "allow_public": allow_public,
    }
    if range_header:
        payload["range"] = range_header
    if quality in {"720", "1080"}:
        payload["quality"] = quality
    return internal_stream_post(
        current_user,
        "/internal/media/file-stream.json",
        payload,
        timeout=30,
    )


def stream_webhard_file_for_viewer(
    viewer_user_id: str,
    viewer_is_admin: bool,
    file_id: int,
    file_kind: str,
    allow_public: bool = False,
    range_header: str = "",
    quality: str = "",
) -> requests.Response:
    payload: dict[str, Any] = {
        "file_id": file_id,
        "file_kind": file_kind,
        "viewer_user_id": viewer_user_id,
        "viewer_is_admin": viewer_is_admin,
        "allow_public": allow_public,
    }
    if range_header:
        payload["range"] = range_header
    if quality in {"720", "1080"}:
        payload["quality"] = quality
    base_url, headers = internal_request_config(None)
    try:
        response = internal_session().post(
            f"{base_url}/internal/media/file-stream.json",
            headers=headers,
            json=payload,
            timeout=30,
            stream=True,
        )
    except requests.RequestException as exc:
        raise RuntimeError("webhard internal stream request failed") from exc
    if not response.ok:
        response.close()
        raise RuntimeError(f"webhard internal stream failed: HTTP {response.status_code}")
    return response


def purge_deleted_webhard_media(current_user: CurrentUser) -> int:
    collection = media_collection()
    query = {} if current_user.is_admin else {"owner_user_id": current_user.user_id}
    existing_ids = [int(item) for item in collection.distinct("webhard_file_id", query) if item]
    if not existing_ids:
        return 0
    active_ids: set[int] = set()
    for index in range(0, len(existing_ids), 500):
        active_ids.update(fetch_active_webhard_file_ids(current_user, existing_ids[index:index + 500]))
    stale_ids = [file_id for file_id in existing_ids if file_id not in active_ids]
    if not stale_ids:
        return 0
    deleted_count = collection.delete_many({"webhard_file_id": {"$in": stale_ids}, **query}).deleted_count
    if deleted_count:
        media_user_state_collection().delete_many({"webhard_file_id": {"$in": stale_ids}})
    return deleted_count


def fetch_active_webhard_file_ids(current_user: CurrentUser, file_ids: list[int]) -> set[int]:
    if not file_ids:
        return set()
    data = internal_post(
        current_user,
        "/internal/media/active-ids.json",
        {
            "viewer_user_id": current_user.user_id,
            "viewer_is_admin": current_user.is_admin,
            "file_ids": file_ids[:500],
        },
    )
    items = data.get("file_ids") or []
    return {int(item) for item in items if item}


def register_youtube_file(current_user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    data = internal_post(
        current_user,
        "/internal/media/register-youtube.json",
        {
            **payload,
            "owner_user_id": current_user.user_id,
        },
        timeout=30,
    )
    file_id = int(data.get("file_id") or 0)
    if file_id <= 0:
        raise RuntimeError("webhard internal register response does not include file_id")
    return data


def mark_media_public(current_user: CurrentUser, file_ids: list[int]) -> dict[str, Any]:
    if not file_ids:
        return {"updated_count": 0}
    updated_count = 0
    updated_ids = []
    for chunk in chunks(file_ids, 500):
        data = internal_post(
            current_user,
            "/internal/media/mark-public.json",
            {
                "owner_user_id": current_user.user_id,
                "file_ids": chunk,
            },
        )
        updated_count += int(data.get("updated_count") or 0)
        updated_ids.extend(int(item) for item in data.get("file_ids") or [] if item)
    return {"updated_count": updated_count, "file_ids": updated_ids}


def set_media_public(current_user: CurrentUser, file_ids: list[int], public: bool) -> dict[str, Any]:
    if not file_ids:
        return {"updated_count": 0, "file_ids": []}
    updated_count = 0
    updated_ids = []
    for chunk in chunks(file_ids, 1000):
        data = internal_post(
            current_user,
            "/internal/media/bulk-public.json",
            {
                "file_ids": chunk,
                "media_public_yn": public,
            },
            timeout=30,
        )
        updated_count += int(data.get("updated_count") or 0)
        updated_ids.extend(int(item) for item in data.get("file_ids") or [] if item)
    return {"updated_count": updated_count, "file_ids": updated_ids}


def check_webhard_internal_ready() -> dict[str, Any]:
    return internal_post(None, "/internal/media/ready.json", {}, timeout=5)


def media_document(row: dict[str, Any], synced_at: datetime, owner_is_admin: bool = False) -> dict[str, Any]:
    file_id = int(row["file_id"])
    content_kind = row.get("content_kind") or "OTHER"
    thumbnail_url = f"/api/media/{file_id}/thumbnail-file/" if row.get("thumbnail_path") else ""
    if not thumbnail_url and content_kind == "IMAGE":
        thumbnail_url = f"/api/media/{file_id}/content-file/"
    webhard_tags = normalize_webhard_tags(row.get("tags"))
    return {
        "webhard_file_id": file_id,
        "owner_user_id": str(row["owner_user_id"]),
        "owner_is_admin": owner_is_admin,
        "file_name": row.get("file_name") or "",
        "display_name": row.get("display_name") or row.get("file_name") or "",
        "webhard_memo": str(row.get("memo") or "").strip()[:2000],
        "webhard_tags": webhard_tags,
        "file_size": int(row.get("file_size") or 0),
        "content_type": row.get("content_type") or "application/octet-stream",
        "content_kind": content_kind,
        "thumbnail_url": thumbnail_url,
        "content_url": f"/api/media/{file_id}/content-file/",
        "download_url": f"/api/media/{file_id}/download-file/",
        "storage_path": row.get("storage_path") or "",
        "original_created_at": row.get("original_created_at"),
        "uploaded_at": row.get("created_at"),
        "webhard_updated_at": row.get("updated_at"),
        "synced_at": synced_at,
    }


def normalize_webhard_tags(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").split(",")
    result = []
    for item in items:
        tag = str(item or "").strip()
        if tag and tag not in result:
            result.append(tag[:40])
    return result[:30]


def internal_post(current_user: CurrentUser | None, path: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    base_url, headers = internal_request_config(current_user)
    try:
        response = internal_session().post(
            f"{base_url}{path}",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError("webhard internal request failed") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"webhard internal response is invalid: HTTP {response.status_code}") from exc
    if not response.ok or body.get("ok") is not True:
        raise RuntimeError(str(body.get("message") or f"webhard internal request failed: HTTP {response.status_code}"))
    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("webhard internal response data is invalid")
    return data


def internal_stream_post(current_user: CurrentUser, path: str, payload: dict[str, Any], timeout: int = 30) -> requests.Response:
    base_url, headers = internal_request_config(current_user)
    try:
        response = internal_session().post(
            f"{base_url}{path}",
            headers=headers,
            json=payload,
            timeout=timeout,
            stream=True,
        )
    except requests.RequestException as exc:
        raise RuntimeError("webhard internal stream request failed") from exc
    if not response.ok:
        response.close()
        raise RuntimeError(f"webhard internal stream failed: HTTP {response.status_code}")
    return response


def internal_request_config(current_user: CurrentUser | None = None) -> tuple[str, dict[str, str]]:
    base_url = str(settings.MEDIA_CONFIG.get("WEBHARD_INTERNAL_BASE_URL") or settings.MEDIA_CONFIG.get("WEBHARD_PUBLIC_BASE_URL") or "").rstrip("/")
    token = str(settings.MEDIA_CONFIG.get("MEDIA_INTERNAL_API_TOKEN") or "").strip()
    if not base_url:
        raise RuntimeError("webhard internal base url is not configured")
    if not token:
        raise RuntimeError("media internal api token is not configured")
    headers = {"X-Internal-Api-Token": token, "Content-Type": "application/json"}
    if current_user is not None:
        if not current_user.access_token:
            raise RuntimeError("admin user token is required for webhard internal request")
        headers["X-User-Access-Token"] = current_user.access_token
    return base_url, headers


def internal_session() -> requests.Session:
    session = getattr(_SESSION_STATE, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=24, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _SESSION_STATE.session = session
    return session


def chunks(items: list[int], size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]
