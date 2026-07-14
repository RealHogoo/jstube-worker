import base64
import json
import logging
import os
import re
import hashlib
import secrets
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bson import ObjectId
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from pymongo.errors import PyMongoError
import qrcode
import qrcode.image.svg

from .auth import CurrentUser, auth_token, require_user
from .job_crypto import encrypt_job_secret
from .mongo import karaoke_pair_attempt_collection, karaoke_queue_collection, karaoke_remote_collection, media_collection, media_user_state_collection, mongo_client
from .webhard import fetch_webhard_file, set_media_public, stream_webhard_file, stream_webhard_file_for_viewer, sync_from_webhard, sync_one_from_webhard
from .youtube import check_download_tools, import_youtube_item, preview_youtube, video_frame_time_tags, youtube_time_tags

try:
    YOUTUBE_IMPORT_CONCURRENCY = max(int(os.getenv("YOUTUBE_IMPORT_CONCURRENCY", "1")), 1)
except ValueError:
    YOUTUBE_IMPORT_CONCURRENCY = 1
RUN_INLINE_JOBS = os.getenv("MEDIA_RUN_INLINE_JOBS", "false").lower() == "true"
YOUTUBE_IMPORT_SEMAPHORE = threading.Semaphore(YOUTUBE_IMPORT_CONCURRENCY)
LOGGER = logging.getLogger(__name__)
SENSITIVE_PATH_RE = re.compile(r"(/karaoke/(?:tv/session|remote)/)[^/]+")
PAIR_ATTEMPT_WINDOW_SECONDS = 5 * 60
PAIR_IP_MAX_FAILURES = 10
PAIR_CODE_MAX_FAILURES = 5
KARAOKE_JOIN_TOKEN_TTL = timedelta(minutes=5)
try:
    TV_SIGNAL_TIMEOUT_SECONDS = max(int(os.getenv("KARAOKE_TV_SIGNAL_TIMEOUT_SECONDS", "300")), 60)
except ValueError:
    TV_SIGNAL_TIMEOUT_SECONDS = 300
try:
    KARAOKE_TV_CLEANUP_INTERVAL_SECONDS = max(int(os.getenv("KARAOKE_TV_CLEANUP_INTERVAL_SECONDS", "60")), 10)
except ValueError:
    KARAOKE_TV_CLEANUP_INTERVAL_SECONDS = 60
try:
    KARAOKE_TV_EXPIRED_QUEUE_RETENTION_HOURS = max(int(os.getenv("KARAOKE_TV_EXPIRED_QUEUE_RETENTION_HOURS", "12")), 1)
except ValueError:
    KARAOKE_TV_EXPIRED_QUEUE_RETENTION_HOURS = 12
_TV_CLEANUP_LOCK = threading.Lock()
_TV_LAST_CLEANUP_MONOTONIC = 0.0


def ok(data: dict[str, Any] | list[Any]) -> JsonResponse:
    return JsonResponse({"ok": True, "code": "OK", "message": "success", "data": data}, json_dumps_params={"ensure_ascii": False})


def bad_request(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "code": "BAD_REQUEST", "message": message}, status=400)


def mongo_unavailable(message: str = "MongoDB connection is unavailable") -> JsonResponse:
    return JsonResponse({"ok": False, "code": "MONGO_UNAVAILABLE", "message": message}, status=503)


def mongo_safe_view(view):
    def wrapped(request: HttpRequest, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PyMongoError as error:
            LOGGER.warning("MongoDB unavailable while handling %s", redact_sensitive_text(request.path), exc_info=error)
            return mongo_unavailable()
    return wrapped


def health(_request: HttpRequest) -> JsonResponse:
    mongo_status = "UP"
    try:
        mongo_client().admin.command("ping")
    except Exception:
        mongo_status = "DOWN"
    return ok({"status": "UP" if mongo_status == "UP" else "DEGRADED", "service": "media-service", "mongo": mongo_status})


def version(_request: HttpRequest) -> JsonResponse:
    return ok({
        "service": "media-service",
        "git_commit": git_commit(),
    })


@csrf_exempt
def options_or_view(request: HttpRequest, view):
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    return view(request)


def me(request: HttpRequest) -> JsonResponse:
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user
    return ok({
        "user_id": user.user_id,
        "roles": user.roles,
        "is_admin": user.is_admin,
        "permissions": {
            "write": user.has_permission("WRITE"),
            "share": user.has_permission("SHARE"),
            "delete": user.has_permission("DELETE"),
        },
    })


def sync(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    limit = int_param(request, "limit", 0)
    return ok(sync_from_webhard(user, limit if limit > 0 else None))


@csrf_exempt
def youtube_preview(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    body = json_body(request)
    url = str(body.get("url") or "").strip()
    if not is_youtube_url(url):
        return bad_request("youtube url is required")
    try:
        return ok(preview_youtube(url))
    except Exception as exc:
        return JsonResponse({"ok": False, "code": "YOUTUBE_ANALYZE_FAILED", "message": str(exc)}, status=502)


@csrf_exempt
def youtube_tools_check(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    return ok(check_download_tools())


@csrf_exempt
def youtube_time_tags_view(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)

    body = json_body(request)
    try:
        default_limit = int(settings.MEDIA_CONFIG.get("YOUTUBE_TIME_TAG_LIMIT") or 100)
        limit = min(max(int(body.get("limit") or default_limit), 1), 100)
    except (TypeError, ValueError):
        limit = 100
    job = create_time_tag_job(user, limit)
    start_time_tag_job(job, user)
    return ok({"job": serialize_time_tag_job(time_tag_job(job["job_id"], user)), **time_tag_job_result(job)})


@csrf_exempt
def youtube_time_tags_status(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    job = time_tag_job(job_id, user)
    if not job:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "time tag job not found"}, status=404)
    return ok({"job": serialize_time_tag_job(job), **time_tag_job_result(job)})


@csrf_exempt
def youtube_import_status(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    raw_ids = body.get("youtube_video_ids") or []
    if not isinstance(raw_ids, list):
        return bad_request("youtube_video_ids must be a list")
    video_ids = []
    for item in raw_ids:
        video_id = str(item or "").strip()
        if video_id and video_id not in video_ids:
            video_ids.append(video_id[:80])
    items = []
    if video_ids:
        query: dict[str, Any] = {
            "source_type": "YOUTUBE_DOWNLOAD",
            "youtube_video_id": {"$in": video_ids[:200]},
        }
        if not user.is_admin:
            query["owner_user_id"] = user.user_id
        items = [serialize_media(item) for item in media_collection().find(query, media_list_projection()).limit(200)]
    return ok({"items": items, "saved_count": len(items), "job": youtube_import_job(job_id, user)})


@csrf_exempt
def youtube_import_view(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    url = str(body.get("url") or "").strip()
    if not is_youtube_url(url):
        return bad_request("youtube url is required")
    tool_status = check_download_tools()
    if not tool_status.get("ok_to_download"):
        return JsonResponse({"ok": False, "code": "YOUTUBE_TOOL_CHECK_FAILED", "message": "download environment or webhard check failed", "data": tool_status}, status=422)
    return ok(create_youtube_import_job(url, user, normalize_tags(body.get("tags") or "")))


@csrf_exempt
def youtube_import_item_start(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    video_id = str(body.get("youtube_video_id") or "").strip()
    if not job_id or not video_id:
        return bad_request("job_id and youtube_video_id are required")
    if not is_safe_youtube_video_id(video_id):
        return bad_request("youtube_video_id is invalid")
    job = youtube_job_collection().find_one({"job_id": job_id, "owner_user_id": user.user_id})
    if not job:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "youtube import job not found"}, status=404)
    try:
        started = start_youtube_import_item(job, video_id, user)
    except RuntimeError as exc:
        return bad_request(str(exc))
    return ok({"job": serialize_youtube_job(started), "message": "youtube item started"})


@csrf_exempt
def youtube_import_start_all(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        return bad_request("job_id is required")
    job = youtube_job_collection().find_one({"job_id": job_id, "owner_user_id": user.user_id})
    if not job:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "youtube import job not found"}, status=404)
    started = start_youtube_import_job(job, user)
    refreshed = youtube_job_collection().find_one({"job_id": job_id}) or job
    return ok({"job": serialize_youtube_job(refreshed), "started_count": started})


def media_list(request: HttpRequest) -> JsonResponse:
    content_kind = request.GET.get("content_kind", "").strip().upper()
    public_karaoke = public_karaoke_requested(content_kind, request.GET.get("public"))
    user = require_user(request, require_media_permission=not public_karaoke)
    if not isinstance(user, CurrentUser):
        return user

    limit = min(max(int_param(request, "limit", 40), 1), 100)
    offset = max(int_param(request, "offset", 0), 0)
    query: dict[str, Any] = readable_media_query(user)
    if user.is_admin and request.GET.get("owner_user_id"):
        query["owner_user_id"] = request.GET["owner_user_id"].strip()

    if content_kind in {"IMAGE", "VIDEO"}:
        query["content_kind"] = content_kind
        if content_kind == "VIDEO" and request.GET.get("tag", "").strip() != "노래방":
            query["tags"] = {"$ne": "노래방"}
    elif content_kind == "KARAOKE":
        query["content_kind"] = "VIDEO"
        query["tags"] = "노래방"
        if public_karaoke:
            query["owner_is_admin"] = True
    if request.GET.get("tag"):
        query["tags"] = request.GET["tag"].strip()
    if request.GET.get("album"):
        query["album"] = request.GET["album"].strip()
    if request.GET.get("favorite") in {"true", "1", "Y"}:
        favorite_ids = favorite_media_ids(user)
        query["webhard_file_id"] = {"$in": favorite_ids}
    if request.GET.get("q"):
        keyword = request.GET["q"].strip()[:80]
    if request.GET.get("q") and keyword:
        search_fields = [
            "title",
            "display_name",
            "file_name",
            "tags",
            "webhard_tags",
        ]
        if content_kind == "KARAOKE":
            search_fields.extend(["karaoke_number", "karaoke_artist", "artist"])
        else:
            search_fields.extend(["album", "description", "webhard_memo", "channel_name", "owner_user_id"])
        search_query = media_search_query(search_fields, keyword, content_kind == "KARAOKE")
        if "$or" in query:
            access_query = {"$or": query.pop("$or")}
            query["$and"] = [access_query, search_query]
        else:
            query.update(search_query)

    sort_key = request.GET.get("sort", "recent").strip().lower()
    if sort_key == "popular":
        sort = [("view_count", -1), ("like_count", -1), ("original_created_at", -1), ("webhard_file_id", -1)]
    elif sort_key == "liked":
        sort = [("like_count", -1), ("view_count", -1), ("original_created_at", -1), ("webhard_file_id", -1)]
    else:
        sort = [("original_created_at", -1), ("webhard_file_id", -1)]

    collection = media_collection()
    count_base_query = dict(query)
    count_base_query.pop("content_kind", None)
    count_base_query.pop("tags", None)
    counts = None
    if offset == 0 or request.GET.get("include_counts") in {"true", "1", "Y"}:
        counts = media_counts(collection, count_base_query)
    cursor = collection.find(query, media_list_projection()).sort(sort).skip(offset).limit(limit + 1)
    raw_items = list(cursor)
    state_map = user_state_map(user, [int(item.get("webhard_file_id") or 0) for item in raw_items])
    fetched_items = [serialize_media(item, user_state=state_map.get(int(item.get("webhard_file_id") or 0))) for item in raw_items]
    has_more = len(fetched_items) > limit
    items = fetched_items[:limit]
    return ok({
        "items": items,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "counts": counts,
    })


@csrf_exempt
def media_detail(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method not in {"GET", "PATCH"}:
        return bad_request("GET or PATCH is required")

    body = json_body(request) if request.method == "PATCH" else {}
    favorite_only_patch = request.method == "PATCH" and set(body.keys()).issubset({"favorite"})
    user = require_user(request, require_media_permission=not favorite_only_patch)
    if not isinstance(user, CurrentUser):
        return user

    query: dict[str, Any] = readable_media_query(user, webhard_file_id)

    if request.method == "GET":
        item = media_collection().find_one(query)
        if not item:
            return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
        return ok({"item": serialize_media(item, user_state=user_state(user, webhard_file_id))})

    item = media_collection().find_one(query)
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    if favorite_only_patch and not user.has_any_media_permission() and not is_karaoke_media(item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "karaoke media is required"}, status=403)
    edit_fields = {"tags", "album", "title", "description", "channel_name", "subscribed"}
    if edit_fields.intersection(body.keys()) and not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    update: dict[str, Any] = {}
    increments: dict[str, int] = {}
    if "tags" in body:
        update["tags"] = normalize_tags(body["tags"])
    if "album" in body:
        update["album"] = str(body.get("album") or "").strip()[:100]
    if "title" in body:
        update["title"] = str(body.get("title") or "").strip()[:180]
    if "description" in body:
        update["description"] = str(body.get("description") or "").strip()[:2000]
    if "channel_name" in body:
        update["channel_name"] = str(body.get("channel_name") or "").strip()[:120]
    if "subscribed" in body:
        update["subscribed"] = bool(body.get("subscribed"))
    if "favorite" in body:
        set_user_state(user, webhard_file_id, {"favorite": bool(body.get("favorite"))})
    if "liked" in body:
        liked = bool(body.get("liked"))
        previous = user_state(user, webhard_file_id)
        previous_liked = bool(previous.get("liked")) if previous else False
        set_user_state(user, webhard_file_id, {"liked": liked})
        if liked != previous_liked:
            increments["like_count"] = 1 if liked else -1
    if body.get("increment_view"):
        increments["view_count"] = 1

    if update or increments:
        update["updated_at"] = datetime.utcnow()
        patch: dict[str, Any] = {"$set": update}
        if increments:
            patch["$inc"] = increments
        result = media_collection().update_one(query, patch)
        if increments.get("like_count", 0) < 0:
            media_collection().update_one(query, {"$max": {"like_count": 0}})
        if result.matched_count == 0:
            return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    item = media_collection().find_one(query)
    return ok({"item": serialize_media(item, user_state=user_state(user, webhard_file_id))})


@csrf_exempt
def media_bulk_public(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)

    body = json_body(request)
    public = bool_value(body.get("public"))
    query = bulk_public_query(body)
    file_ids = [
        int(file_id)
        for file_id in media_collection().distinct("webhard_file_id", query)
        if file_id
    ]
    webhard_result = set_media_public(user, file_ids, public)
    updated_ids = [int(file_id) for file_id in webhard_result.get("file_ids") or [] if file_id]
    if updated_ids:
        media_collection().update_many(
            {"webhard_file_id": {"$in": updated_ids}},
            {"$set": {"owner_is_admin": public, "updated_at": datetime.utcnow()}},
        )
    return ok({
        "matched_count": len(file_ids),
        "updated_count": len(updated_ids),
        "public": public,
    })


@csrf_exempt
def media_delete(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")

    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.has_permission("DELETE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "delete permission is required"}, status=403)

    item = media_collection().find_one({"webhard_file_id": webhard_file_id})
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    if not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    token = auth_token(request)
    try:
        response = requests.post(
            f"{settings.MEDIA_CONFIG['WEBHARD_PUBLIC_BASE_URL']}/file/delete.json",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"file_id": webhard_file_id},
            timeout=15,
        )
        body = response.json()
    except requests.RequestException:
        return JsonResponse({"ok": False, "code": "WEBHARD_UNAVAILABLE", "message": "webhard delete request failed"}, status=502)
    except ValueError:
        return JsonResponse({"ok": False, "code": "WEBHARD_INVALID_RESPONSE", "message": "webhard delete response is invalid"}, status=502)

    if not response.ok or body.get("ok") is not True:
        return JsonResponse(
            {
                "ok": False,
                "code": body.get("code") or "WEBHARD_DELETE_FAILED",
                "message": body.get("message") or "delete failed",
            },
            status=response.status_code if response.status_code >= 400 else 502,
        )

    media_collection().delete_one({"webhard_file_id": webhard_file_id})
    media_user_state_collection().delete_many({"webhard_file_id": webhard_file_id})
    return ok({"file_id": webhard_file_id})


@csrf_exempt
def media_thumbnail(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")

    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    item = media_collection().find_one({"webhard_file_id": webhard_file_id})
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    if not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    body = json_body(request)
    seek_seconds = optional_seconds(body.get("seek_seconds"))
    if seek_seconds is False:
        return bad_request("seek_seconds must be numeric")

    payload: dict[str, Any] = {"file_id": webhard_file_id, "limit": 1}
    if isinstance(seek_seconds, (int, float)):
        payload["seek_seconds"] = seek_seconds

    token = auth_token(request)
    try:
        response = requests.post(
            f"{settings.MEDIA_CONFIG['WEBHARD_PUBLIC_BASE_URL']}/thumbnail/rebuild.json",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        body = response.json()
    except requests.RequestException:
        return JsonResponse({"ok": False, "code": "WEBHARD_UNAVAILABLE", "message": "webhard thumbnail request failed"}, status=502)
    except ValueError:
        return JsonResponse({"ok": False, "code": "WEBHARD_INVALID_RESPONSE", "message": "webhard thumbnail response is invalid"}, status=502)

    if not response.ok or body.get("ok") is not True:
        return JsonResponse(
            {
                "ok": False,
                "code": body.get("code") or "WEBHARD_THUMBNAIL_FAILED",
                "message": body.get("message") or "thumbnail creation failed",
            },
            status=response.status_code if response.status_code >= 400 else 502,
        )

    synced = sync_one_from_webhard(user, webhard_file_id)
    return ok({"thumbnail": body.get("data") or {}, "item": serialize_media(synced.get("item"))})


@csrf_exempt
def karaoke_remote_session(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    now = datetime.utcnow()
    session_id = uuid.uuid4().hex
    item = {
        "session_id": session_id,
        "owner_user_id": user.user_id,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=12),
        "commands": [],
        "next_sequence": 1,
    }
    karaoke_remote_collection().insert_one(item)
    return ok({"session_id": session_id, "expires_at": item["expires_at"].isoformat()})


@csrf_exempt
def karaoke_tv_session(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")

    now = datetime.utcnow()
    session_id = uuid.uuid4().hex
    pairing_token = secrets.token_urlsafe(32)
    tv_token = secrets.token_urlsafe(32)
    pairing_code = f"{secrets.randbelow(1000000):06d}"
    expires_at = now + timedelta(minutes=5)
    base_url = frontend_base_url(request)
    pair_url = f"{base_url}/?karaoke_pair={pairing_token}"
    item = {
        "session_id": session_id,
        "session_type": "TV",
        "status": "WAITING",
        "pairing_code": pairing_code,
        "pairing_token_hash": token_hash(pairing_token),
        "join_token": "",
        "join_token_hash": "",
        "join_token_expires_at": None,
        "tv_token_hash": token_hash(tv_token),
        "owner_user_id": "",
        "owner_is_admin": False,
        "participants": [],
        "created_at": now,
        "updated_at": now,
        "expires_at": expires_at,
        "last_tv_seen_at": now,
        "paired_at": None,
        "commands": [],
        "next_sequence": 1,
        "queue": [],
        "current_item": None,
        "playback_state": "IDLE",
    }
    karaoke_remote_collection().insert_one(item)
    return ok({
        "session_id": session_id,
        "pairing_code": pairing_code,
        "pairing_token": pairing_token,
        "tv_token": tv_token,
        "pair_url": pair_url,
        "qr_image_url": qr_svg_data_url(pair_url),
        "expires_at": expires_at.isoformat(),
    })


@csrf_exempt
def karaoke_tv_pair(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user

    body = json_body(request)
    pairing_token = str(body.get("pairing_token") or "").strip()
    pairing_code = re.sub(r"\D", "", str(body.get("pairing_code") or ""))[:6]
    if not pairing_token and not pairing_code:
        return bad_request("pairing token or code is required")

    now = datetime.utcnow()
    pair_rate_keys = pairing_attempt_keys(request, pairing_code, pairing_token)
    if is_pairing_rate_limited(pair_rate_keys, now):
        return JsonResponse({"ok": False, "code": "RATE_LIMITED", "message": "pairing attempt rate limit exceeded"}, status=429)

    query: dict[str, Any] = {"session_type": "TV", "status": "WAITING", "expires_at": {"$gt": now}}
    if pairing_token:
        query["pairing_token_hash"] = token_hash(pairing_token)
    else:
        query["pairing_code"] = pairing_code
    session = karaoke_remote_collection().find_one(query)
    if not session:
        record_pairing_failure(pair_rate_keys, now)
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV pairing session was not found or expired"}, status=404)

    expires_at = now + timedelta(hours=12)
    ensure_account_queue(user.user_id, now)
    command = {
        "sequence": int(session.get("next_sequence") or 1),
        "type": "PAIRED",
        "payload": {"message": "모바일 기기가 연결되었습니다."},
        "created_by": user.user_id,
        "created_at": now,
    }
    karaoke_remote_collection().update_one(
        {"_id": session["_id"]},
        {
            "$set": {
                "status": "PAIRED",
                "owner_user_id": user.user_id,
                "owner_is_admin": user.is_admin,
                "paired_at": now,
                "last_tv_seen_at": now,
                "updated_at": now,
                "expires_at": expires_at,
                "join_token": "",
                "join_token_hash": "",
                "join_token_expires_at": None,
                "pairing_token_hash": "",
                "pairing_code": "",
                "commands": [command],
                "next_sequence": command["sequence"] + 1,
            }
        },
    )
    return ok({
        "session_id": session["session_id"],
        "expires_at": expires_at.isoformat(),
    })


@csrf_exempt
def karaoke_tv_join(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user

    body = json_body(request)
    session_id = str(body.get("session_id") or "").strip()
    join_token = str(body.get("join_token") or "").strip()
    if not session_id or not join_token:
        return bad_request("session_id and join_token are required")

    now = datetime.utcnow()
    collection = karaoke_remote_collection()
    session = collection.find_one({
        "session_id": session_id,
        "session_type": "TV",
        "status": "PAIRED",
        "join_token_hash": token_hash(join_token),
        "expires_at": {"$gt": now},
        "join_token_expires_at": {"$gt": now},
    })
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "초대 링크가 만료되었거나 사용할 수 없습니다."}, status=404)
    if tv_session_is_stale(session, now):
        expire_tv_session(session, now)
        return JsonResponse({"ok": False, "code": "TV_SESSION_EXPIRED", "message": "TV signal was lost"}, status=410)

    participant = {
        "user_id": user.user_id,
        "joined_at": now,
        "last_seen_at": now,
    }
    collection.update_one(
        {"_id": session["_id"]},
        {
            "$pull": {"participants": {"user_id": user.user_id}},
        },
    )
    collection.update_one(
        {"_id": session["_id"]},
        {
            "$push": {"participants": {"$each": [participant], "$slice": -50}},
            "$set": {"updated_at": now, "expires_at": now + timedelta(hours=12)},
        },
    )
    role = "HOST" if str(session.get("owner_user_id") or "") == user.user_id else "GUEST"
    return ok({"session_id": session_id, "role": role, "expires_at": (now + timedelta(hours=12)).isoformat()})


def karaoke_tv_status(request: HttpRequest, tv_token: str) -> JsonResponse | HttpResponse:
    if request.method != "GET":
        return bad_request("GET is required")
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV session was not found"}, status=404)
    return ok(serialize_tv_session(session))


def karaoke_tv_commands(request: HttpRequest, tv_token: str) -> JsonResponse | HttpResponse:
    if request.method != "GET":
        return bad_request("GET is required")
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV session was not found"}, status=404)
    after = int_param(request, "after", 0)
    commands = [
        serialize_tv_command(command)
        for command in session.get("commands") or []
        if int(command.get("sequence") or 0) > after
    ]
    data = serialize_tv_session(session)
    data.update({
        "commands": commands,
        "latest_sequence": int(session.get("next_sequence") or 1) - 1,
    })
    return ok(data)


def karaoke_tv_events(request: HttpRequest, tv_token: str) -> JsonResponse | HttpResponse:
    if request.method != "GET":
        return bad_request("GET is required")
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV session was not found"}, status=404)
    after = int_param(request, "after", 0)
    response = StreamingHttpResponse(
        stream_tv_events(session, after),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_exempt
def karaoke_tv_next(request: HttpRequest, tv_token: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV session was not found"}, status=404)
    if session.get("status") != "PAIRED":
        return JsonResponse({"ok": False, "code": "UNAUTHORIZED", "message": "paired TV session is required"}, status=401)

    sequence = int(session.get("next_sequence") or 1)
    now = datetime.utcnow()
    command = {
        "sequence": sequence,
        "type": "NEXT",
        "payload": {},
        "created_by": "TV",
        "created_at": now,
    }
    commands = list(session.get("commands") or [])[-49:] + [command]
    state_patch = remote_state_patch(account_queue_state(session), "NEXT", {})
    save_account_queue_state(session, state_patch, now)
    session_patch = {
        "commands": commands,
        "updated_at": now,
        "expires_at": now + timedelta(hours=12),
        "last_tv_seen_at": now,
        "next_sequence": sequence + 1,
    }
    collection = karaoke_remote_collection()
    collection.update_one({"_id": session["_id"]}, {"$set": session_patch})
    refreshed = collection.find_one({"_id": session["_id"]}) or session
    return ok({
        "session": serialize_tv_session(refreshed),
        "latest_sequence": sequence,
    })


@csrf_exempt
def karaoke_tv_heartbeat(request: HttpRequest, tv_token: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "TV session was not found"}, status=404)
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=12 if session.get("status") == "PAIRED" else 5 / 60)
    karaoke_remote_collection().update_one(
        {"_id": session["_id"]},
        {"$set": {"updated_at": now, "last_tv_seen_at": now, "expires_at": expires_at}},
    )
    return ok({"session_id": session["session_id"], "expires_at": expires_at.isoformat()})


def karaoke_tv_media_file_proxy(request: HttpRequest, tv_token: str, webhard_file_id: int) -> JsonResponse | HttpResponse:
    cleanup_stale_tv_sessions()
    session = tv_session_by_token(tv_token)
    if not session or session.get("status") != "PAIRED" or not session.get("owner_user_id"):
        return JsonResponse({"ok": False, "code": "UNAUTHORIZED", "message": "paired TV session is required"}, status=401)
    if not tv_media_file_allowed(session, webhard_file_id):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "TV token can only stream current or queued media"}, status=403)
    tv_user = CurrentUser(
        user_id=str(session.get("owner_user_id") or ""),
        roles=["ROLE_ADMIN"] if session.get("owner_is_admin") else [],
        service_permissions={"MEDIA_SERVICE": ["READ"]},
    )
    item = media_collection().find_one(readable_media_query(tv_user, webhard_file_id))
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media file not found"}, status=404)
    range_header = normalized_range_header(request.headers.get("Range", ""))
    if range_header is None:
        return JsonResponse({"ok": False, "code": "INVALID_RANGE", "message": "range header is invalid"}, status=416)
    started_at = time.monotonic()
    stream_meta = {
        "session_id": str(session.get("session_id") or ""),
        "webhard_file_id": int(webhard_file_id),
        "range": range_header or "",
        "before": cgroup_memory_snapshot(),
    }
    try:
        upstream = stream_webhard_file_for_viewer(
            tv_user.user_id,
            tv_user.is_admin,
            webhard_file_id,
            "content",
            allow_public=True,
            range_header=range_header,
            quality=playback_quality(request, default="1080"),
        )
    except RuntimeError as exc:
        LOGGER.warning("TV stream failed for file_id=%s: %s", webhard_file_id, exc)
        return JsonResponse({"ok": False, "code": "WEBHARD_STREAM_FAILED", "message": "media stream failed"}, status=502)

    content_length = int_header(upstream.headers.get("Content-Length"))
    content_range = str(upstream.headers.get("Content-Range") or "")
    LOGGER.info(
        "karaoke_tv_stream_start session=%s file_id=%s status=%s range=%s content_length=%s content_range=%s memory=%s",
        redact_token(stream_meta["session_id"]),
        webhard_file_id,
        upstream.status_code,
        range_header or "-",
        content_length,
        content_range or "-",
        stream_meta["before"],
    )
    response = StreamingHttpResponse(
        stream_response_chunks(
            upstream,
            on_close=lambda sent_bytes, error=None: log_karaoke_stream_finish(stream_meta, started_at, upstream.status_code, sent_bytes, error),
        ),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type") or "application/octet-stream",
    )
    for header in ["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "X-Content-Type-Options"]:
        value = upstream.headers.get(header)
        if value:
            response[header] = value
    return response


@csrf_exempt
def karaoke_remote_command(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user

    body = json_body(request)
    command_type = str(body.get("type") or "").strip().upper()
    if command_type not in {"PLAY_ITEM", "RESERVE_ITEM", "NEXT", "PREVIOUS", "PREV_TAG", "NEXT_TAG", "TOGGLE_PLAY", "CLEAR_QUEUE"}:
        return bad_request("invalid remote command")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

    collection = karaoke_remote_collection()
    session = collection.find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if session.get("session_type") == "TV" and tv_session_is_stale(session, datetime.utcnow()):
        expire_tv_session(session, datetime.utcnow())
        return JsonResponse({"ok": False, "code": "TV_SESSION_EXPIRED", "message": "TV signal was lost"}, status=410)
    role = remote_session_role(session, user)
    if not role:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)
    if role == "GUEST" and command_type != "RESERVE_ITEM":
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "jam guests can reserve songs only"}, status=403)
    if is_remote_rate_limited(session):
        return JsonResponse({"ok": False, "code": "RATE_LIMITED", "message": "remote command rate limit exceeded"}, status=429)
    sequence = int(session.get("next_sequence") or 1)
    payload_user = remote_payload_user(session, user)
    sanitized_payload = sanitize_remote_payload(payload_user, payload)
    if command_type == "RESERVE_ITEM":
        reserved_by = remote_reserver_label(role, user)
        item = sanitized_payload.get("item") if isinstance(sanitized_payload.get("item"), dict) else None
        if item:
            item["reserved_by"] = reserved_by
            item["reserved_by_user_id"] = user.user_id
            item["reserved_by_role"] = role
        if queue_has_media_item(account_queue_state(session), int((item or {}).get("webhard_file_id") or 0)):
            return JsonResponse(
                {"ok": False, "code": "DUPLICATE_RESERVATION", "message": "이미 예약된 곡입니다."},
                status=409,
            )
    command = {
        "sequence": sequence,
        "type": command_type,
        "payload": sanitized_payload,
        "created_by": user.user_id,
        "created_role": role,
        "created_at": datetime.utcnow(),
    }
    commands = list(session.get("commands") or [])[-49:] + [command]
    state_patch = remote_state_patch(account_queue_state(session), command_type, sanitized_payload)
    save_account_queue_state(session, state_patch, command["created_at"])
    session_patch = {
        "commands": commands,
        "updated_at": command["created_at"],
        "expires_at": command["created_at"] + timedelta(hours=12),
        "next_sequence": sequence + 1,
    }
    collection.update_one(
        {"session_id": session_id},
        {"$set": session_patch},
    )
    return ok({"sequence": sequence})


@csrf_exempt
def karaoke_remote_join_token(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user
    collection = karaoke_remote_collection()
    session = collection.find_one({"session_id": session_id, "session_type": "TV", "status": "PAIRED"})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if remote_session_role(session, user) != "HOST":
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "host permission is required"}, status=403)
    now = datetime.utcnow()
    join_token = secrets.token_urlsafe(32)
    join_token_expires_at = now + KARAOKE_JOIN_TOKEN_TTL
    collection.update_one(
        {"_id": session["_id"]},
        {
            "$set": {
                "join_token": join_token,
                "join_token_hash": token_hash(join_token),
                "join_token_expires_at": join_token_expires_at,
                "updated_at": now,
            }
        },
    )
    return ok({
        "session_id": session_id,
        "join_token": join_token,
        "join_token_expires_at": join_token_expires_at.isoformat(),
    })


@csrf_exempt
def karaoke_remote_heartbeat(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user
    session = karaoke_remote_collection().find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if session.get("session_type") == "TV" and tv_session_is_stale(session, datetime.utcnow()):
        expire_tv_session(session, datetime.utcnow())
        return JsonResponse({"ok": False, "code": "TV_SESSION_EXPIRED", "message": "TV signal was lost"}, status=410)
    if not remote_session_role(session, user):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)
    expires_at = datetime.utcnow() + timedelta(hours=12)
    karaoke_remote_collection().update_one(
        {"session_id": session_id},
        {"$set": {"updated_at": datetime.utcnow(), "expires_at": expires_at}},
    )
    return ok({"session_id": session_id, "expires_at": expires_at.isoformat()})


def karaoke_remote_commands(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method != "GET":
        return bad_request("GET is required")
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user

    session = karaoke_remote_collection().find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if session.get("session_type") == "TV" and tv_session_is_stale(session, datetime.utcnow()):
        expire_tv_session(session, datetime.utcnow())
        return JsonResponse({"ok": False, "code": "TV_SESSION_EXPIRED", "message": "TV signal was lost"}, status=410)
    if not remote_session_role(session, user):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)

    after = int_param(request, "after", 0)
    commands = [
        serialize_remote_command(command)
        for command in session.get("commands") or []
        if int(command.get("sequence") or 0) > after
    ]
    return ok({"session_id": session_id, "commands": commands, "latest_sequence": int(session.get("next_sequence") or 1) - 1})


def media_file_proxy(request: HttpRequest, webhard_file_id: int, file_kind: str) -> JsonResponse | HttpResponse:
    user = require_user(request, require_media_permission=False)
    if not isinstance(user, CurrentUser):
        return user

    item = media_collection().find_one(readable_media_query(user, webhard_file_id))
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media file not found"}, status=404)
    if not user.has_any_media_permission() and not is_public_karaoke_media(item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "media permission is required"}, status=403)

    if file_kind not in {"thumbnail", "content", "download"}:
        return bad_request("invalid file kind")
    range_header = ""
    if file_kind == "content":
        range_header = normalized_range_header(request.headers.get("Range", ""))
        if range_header is None:
            return JsonResponse({"ok": False, "code": "INVALID_RANGE", "message": "range header is invalid"}, status=416)
    quality = playback_quality(request) if file_kind == "content" else ""
    try:
        upstream = stream_webhard_file(
            user,
            webhard_file_id,
            file_kind,
            allow_public=is_public_media(item),
            range_header=range_header,
            quality=quality,
        )
    except RuntimeError as exc:
        LOGGER.warning("webhard stream failed for file_id=%s kind=%s: %s", webhard_file_id, file_kind, exc)
        return JsonResponse({"ok": False, "code": "WEBHARD_STREAM_FAILED", "message": "media stream failed"}, status=502)

    response = StreamingHttpResponse(
        stream_response_chunks(upstream),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type") or "application/octet-stream",
    )
    for header in [
        "Content-Disposition",
        "X-Content-Type-Options",
        "Content-Security-Policy",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
    ]:
        value = upstream.headers.get(header)
        if value:
            response[header] = value
    return response


def albums(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    query = readable_media_query(user)
    values = [item for item in media_collection().distinct("album", query) if item]
    values.sort()
    return ok({"items": values})


def serialize_media(item: dict[str, Any] | None, user_state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not item:
        return {}
    result = dict(item)
    result["_id"] = str(result.get("_id", ""))
    result["title"] = result.get("title") or result.get("display_name") or result.get("file_name") or "Untitled"
    result["description"] = result.get("description") or ""
    result["channel_name"] = result.get("channel_name") or channel_name(result)
    result["view_count"] = max(int(result.get("view_count") or 0), 0)
    result["like_count"] = max(int(result.get("like_count") or 0), 0)
    state = user_state or {}
    result["favorite"] = bool(state.get("favorite"))
    result["liked"] = bool(state.get("liked"))
    result["subscribed"] = bool(result.get("subscribed"))
    result["karaoke_number"] = karaoke_number(result)
    result["karaoke_artist"] = karaoke_artist(result)
    result["time_markers"] = karaoke_time_markers(result.get("tags") or [])
    thumbnail_url = str(result.get("thumbnail_url") or "")
    content_url = str(result.get("content_url") or "")
    if result.get("content_kind") == "VIDEO" and (thumbnail_url == content_url or "/file/content/" in thumbnail_url):
        result["thumbnail_url"] = ""
    result.pop("storage_path", None)
    for key, value in list(result.items()):
        if isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def media_list_projection() -> dict[str, int]:
    return {
        "_id": 1,
        "webhard_file_id": 1,
        "owner_user_id": 1,
        "owner_is_admin": 1,
        "file_name": 1,
        "display_name": 1,
        "file_size": 1,
        "content_type": 1,
        "content_kind": 1,
        "thumbnail_url": 1,
        "content_url": 1,
        "download_url": 1,
        "storage_path": 1,
        "original_created_at": 1,
        "uploaded_at": 1,
        "webhard_updated_at": 1,
        "source_type": 1,
        "youtube_video_id": 1,
        "youtube_url": 1,
        "youtube_playlist_id": 1,
        "youtube_playlist_title": 1,
        "title": 1,
        "description": 1,
        "channel_name": 1,
        "album": 1,
        "tags": 1,
        "webhard_tags": 1,
        "webhard_memo": 1,
        "favorite": 1,
        "view_count": 1,
        "like_count": 1,
        "liked": 1,
        "subscribed": 1,
    }


def favorite_media_ids(user: CurrentUser) -> list[int]:
    return [
        int(item)
        for item in media_user_state_collection().distinct("webhard_file_id", {"user_id": user.user_id, "favorite": True})
        if item
    ]


def user_state(user: CurrentUser, webhard_file_id: int) -> dict[str, Any] | None:
    return media_user_state_collection().find_one({"user_id": user.user_id, "webhard_file_id": int(webhard_file_id)})


def user_state_map(user: CurrentUser, webhard_file_ids: list[int]) -> dict[int, dict[str, Any]]:
    ids = [int(item) for item in webhard_file_ids if item]
    if not ids:
        return {}
    states = media_user_state_collection().find({"user_id": user.user_id, "webhard_file_id": {"$in": ids}})
    return {int(state.get("webhard_file_id")): state for state in states if state.get("webhard_file_id")}


def set_user_state(user: CurrentUser, webhard_file_id: int, patch: dict[str, Any]) -> None:
    update = {"updated_at": datetime.utcnow(), **patch}
    media_user_state_collection().update_one(
        {"user_id": user.user_id, "webhard_file_id": int(webhard_file_id)},
        {
            "$set": update,
            "$setOnInsert": {
                "user_id": user.user_id,
                "webhard_file_id": int(webhard_file_id),
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )


def readable_media_query(user: CurrentUser, webhard_file_id: int | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if webhard_file_id is not None:
        query["webhard_file_id"] = webhard_file_id
    if user.is_admin:
        return query
    query["$or"] = [
        {"owner_user_id": user.user_id},
        {"owner_is_admin": True},
    ]
    return query


def bulk_public_query(body: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {"content_kind": "VIDEO"}
    if bool_value(body.get("karaoke_only")):
        query["tags"] = "노래방"
    return query


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "y", "yes", "on"}


def public_karaoke_requested(content_kind: str, public_value: Any = None) -> bool:
    if str(content_kind or "").strip().upper() != "KARAOKE":
        return False
    return str(public_value if public_value is not None else "true").strip().lower() not in {"false", "0", "n", "no", "off"}


def is_public_media(item: dict[str, Any]) -> bool:
    return bool(item.get("owner_is_admin"))


def is_karaoke_media(item: dict[str, Any]) -> bool:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    return str(item.get("content_kind") or "").upper() == "VIDEO" and "노래방" in tags


def is_public_karaoke_media(item: dict[str, Any]) -> bool:
    return is_public_media(item) and is_karaoke_media(item)


def stream_tv_events(initial_session: dict[str, Any], after: int):
    session_id = initial_session.get("session_id") or ""
    latest_sent = int(after or 0)
    last_ping_at = 0.0
    yield "event: ready\ndata: {}\n\n"
    while True:
        session = karaoke_remote_collection().find_one({"session_id": session_id, "session_type": "TV"})
        now = datetime.utcnow()
        if not session:
            yield sse_event("error", {"code": "NOT_FOUND", "message": "TV session was not found"})
            return
        if tv_session_is_stale(session, now):
            expire_tv_session(session, now)
            yield sse_event("error", {"code": "TV_SESSION_EXPIRED", "message": "TV signal was lost"})
            return
        commands = [
            serialize_tv_command(command)
            for command in session.get("commands") or []
            if int(command.get("sequence") or 0) > latest_sent
        ]
        latest_sequence = int(session.get("next_sequence") or 1) - 1
        if commands or latest_sequence > latest_sent:
            data = serialize_tv_session(session)
            data.update({
                "commands": commands,
                "latest_sequence": latest_sequence,
            })
            latest_sent = max(latest_sent, latest_sequence)
            yield sse_event("message", data)
        elif time.monotonic() - last_ping_at >= 15:
            last_ping_at = time.monotonic()
            yield sse_event("ping", {"ts": now.isoformat()})
        time.sleep(1)


def sse_event(event_name: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {payload}\n\n"


def stream_response_chunks(upstream: requests.Response, on_close=None):
    sent_bytes = 0
    error = None
    try:
        for chunk in upstream.iter_content(chunk_size=1024 * 1024):
            if chunk:
                sent_bytes += len(chunk)
                yield chunk
    except Exception as exc:
        error = exc
        raise
    finally:
        upstream.close()
        if on_close:
            on_close(sent_bytes, error)


def normalized_range_header(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"bytes=(\d{0,16})-(\d{0,16})", text)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    return text


def playback_quality(request: HttpRequest, default: str = "") -> str:
    quality = str(request.GET.get("quality") or default or "").strip()
    return quality if quality in {"720", "1080"} else ""


def cleanup_stale_tv_sessions(force: bool = False) -> dict[str, int]:
    global _TV_LAST_CLEANUP_MONOTONIC
    now_monotonic = time.monotonic()
    if not force and now_monotonic - _TV_LAST_CLEANUP_MONOTONIC < KARAOKE_TV_CLEANUP_INTERVAL_SECONDS:
        return {"expired_count": 0, "queue_clear_count": 0}
    if not _TV_CLEANUP_LOCK.acquire(blocking=False):
        return {"expired_count": 0, "queue_clear_count": 0}
    try:
        _TV_LAST_CLEANUP_MONOTONIC = now_monotonic
        now = datetime.utcnow()
        stale_before = now - timedelta(seconds=TV_SIGNAL_TIMEOUT_SECONDS)
        collection = karaoke_remote_collection()
        expired_result = collection.update_many(
            {
                "session_type": "TV",
                "status": "PAIRED",
                "$or": [
                    {"last_tv_seen_at": {"$lt": stale_before}},
                    {"last_tv_seen_at": {"$exists": False}, "updated_at": {"$lt": stale_before}},
                ],
            },
            {"$set": {"status": "EXPIRED", "updated_at": now, "expires_at": now}},
        )
        queue_retention_before = now - timedelta(hours=KARAOKE_TV_EXPIRED_QUEUE_RETENTION_HOURS)
        expired_sessions = list(collection.find(
            {
                "session_type": "TV",
                "status": {"$in": ["EXPIRED", "CLOSED"]},
                "updated_at": {"$lt": queue_retention_before},
                "owner_user_id": {"$ne": ""},
            },
            {"owner_user_id": 1},
        ).limit(200))
        owners = sorted({str(item.get("owner_user_id") or "").strip() for item in expired_sessions if item.get("owner_user_id")})
        queue_clear_count = 0
        if owners:
            queue_clear_count = karaoke_queue_collection().update_many(
                {"owner_user_id": {"$in": owners}, "updated_at": {"$lt": queue_retention_before}},
                {"$set": {"queue": [], "current_item": None, "playback_state": "IDLE", "updated_at": now}},
            ).modified_count
        if expired_result.modified_count or queue_clear_count:
            LOGGER.info(
                "karaoke_tv_cleanup expired_sessions=%s cleared_queues=%s timeout_seconds=%s queue_retention_hours=%s",
                expired_result.modified_count,
                queue_clear_count,
                TV_SIGNAL_TIMEOUT_SECONDS,
                KARAOKE_TV_EXPIRED_QUEUE_RETENTION_HOURS,
            )
        return {"expired_count": expired_result.modified_count, "queue_clear_count": queue_clear_count}
    finally:
        _TV_CLEANUP_LOCK.release()


def int_header(value: str | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def cgroup_memory_snapshot() -> dict[str, int]:
    snapshot: dict[str, int] = {}
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            if os.path.exists(path):
                snapshot["current"] = int(Path(path).read_text().strip())
                break
        except Exception:
            pass
    stat_paths = ("/sys/fs/cgroup/memory.stat", "/sys/fs/cgroup/memory/memory.stat")
    wanted = {"anon", "file", "kernel", "slab", "rss", "cache"}
    for path in stat_paths:
        try:
            if not os.path.exists(path):
                continue
            for line in Path(path).read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] in wanted:
                    snapshot[parts[0]] = int(parts[1])
            break
        except Exception:
            continue
    return snapshot


def log_karaoke_stream_finish(stream_meta: dict[str, Any], started_at: float, status_code: int, sent_bytes: int, error=None) -> None:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    log_fn = LOGGER.warning if error else LOGGER.info
    log_fn(
        "karaoke_tv_stream_finish session=%s file_id=%s status=%s sent_bytes=%s elapsed_ms=%s range=%s memory_before=%s memory_after=%s error=%s",
        redact_token(str(stream_meta.get("session_id") or "")),
        stream_meta.get("webhard_file_id"),
        status_code,
        sent_bytes,
        elapsed_ms,
        stream_meta.get("range") or "-",
        stream_meta.get("before") or {},
        cgroup_memory_snapshot(),
        error.__class__.__name__ if error else "",
    )


def redact_token(value: str) -> str:
    text = str(value or "")
    if len(text) <= 10:
        return text
    return f"{text[:6]}...{text[-4:]}"


def media_counts(collection, query: dict[str, Any]) -> dict[str, int]:
    tags_array = {"$cond": [{"$isArray": "$tags"}, "$tags", []]}
    karaoke_tag = {"$in": ["노래방", tags_array]}
    row = next(collection.aggregate([
        {"$match": query},
        {
            "$group": {
                "_id": None,
                "image": {"$sum": {"$cond": [{"$eq": ["$content_kind", "IMAGE"]}, 1, 0]}},
                "video": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$content_kind", "VIDEO"]}, {"$not": [karaoke_tag]}]},
                            1,
                            0,
                        ]
                    }
                },
                "karaoke": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$content_kind", "VIDEO"]}, karaoke_tag]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]), None) or {}
    return {
        "image": int(row.get("image") or 0),
        "video": int(row.get("video") or 0),
        "karaoke": int(row.get("karaoke") or 0),
    }


def time_tag_job_collection():
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["youtube_time_tag_jobs"]
    collection.create_index([("job_id", 1)], unique=True)
    collection.create_index([("owner_user_id", 1), ("updated_at", -1)])
    collection.create_index([("status", 1), ("updated_at", -1)])
    return collection


def create_time_tag_job(user: CurrentUser, limit: int) -> dict[str, Any]:
    running = time_tag_job_collection().find_one({
        "owner_user_id": user.user_id,
        "status": {"$in": ["QUEUED", "RUNNING"]},
    })
    if running:
        return running
    items = select_time_tag_items(limit)
    now = datetime.utcnow()
    job = {
        "job_id": uuid.uuid4().hex,
        "owner_user_id": user.user_id,
        "owner_roles": user.roles,
        "owner_service_permissions": user.service_permissions,
        "owner_access_token": encrypt_job_secret(user.access_token),
        "status": "QUEUED",
        "message": "time tag job created",
        "item_count": len(items),
        "checked_count": 0,
        "updated_count": 0,
        "failed_count": 0,
        "skipped_existing_count": 0,
        "skipped_empty_count": 0,
        "skipped_missing_url_count": 0,
        "skipped_missing_file_count": 0,
        "items": items,
        "created_at": now,
        "updated_at": now,
    }
    time_tag_job_collection().insert_one(job)
    return job


def select_time_tag_items(limit: int) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "content_kind": "VIDEO",
        "tags": "노래방",
    }
    skipped_file_ids = skipped_empty_time_tag_file_ids()
    selected = []
    scanned_count = 0
    for item in media_collection().find(query, media_list_projection()).sort("updated_at", -1).limit(limit * 10):
        scanned_count += 1
        file_id = int(item.get("webhard_file_id") or 0)
        if file_id in skipped_file_ids:
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        if karaoke_time_markers(tags):
            continue
        selected.append({
            "order_no": len(selected) + 1,
            "file_id": file_id,
            "title": media_item_log_label(item),
            "status": "QUEUED",
            "message": "",
            "tags": [],
            "started_at": None,
            "finished_at": None,
        })
        if len(selected) >= limit:
            break
    LOGGER.warning("youtube time tag job selected scanned=%s queued=%s", scanned_count, len(selected))
    return selected


def skipped_empty_time_tag_file_ids() -> set[int]:
    file_ids: set[int] = set()
    rows = time_tag_job_collection().aggregate([
        {"$unwind": "$items"},
        {"$match": {"items.status": "SKIPPED_EMPTY"}},
        {"$group": {"_id": "$items.file_id"}},
    ])
    for row in rows:
        try:
            file_ids.add(int(row.get("_id") or 0))
        except (TypeError, ValueError):
            continue
    return file_ids


def time_tag_job(job_id: str, user: CurrentUser) -> dict[str, Any] | None:
    if not job_id:
        return None
    query = {"job_id": job_id}
    if not user.is_admin:
        query["owner_user_id"] = user.user_id
    return time_tag_job_collection().find_one(query)


def start_time_tag_job(job: dict[str, Any], user: CurrentUser) -> None:
    if not job or job.get("worker_running"):
        return
    if not any(item.get("status") in {"QUEUED", "FAILED"} for item in job.get("items") or []):
        refresh_time_tag_job_status(str(job.get("job_id") or ""))
        return
    if not RUN_INLINE_JOBS:
        return
    now = datetime.utcnow()
    updated = time_tag_job_collection().update_one(
        {"job_id": job["job_id"], "worker_running": {"$ne": True}},
        {"$set": {"worker_running": True, "status": "RUNNING", "message": "time tag generation running", "updated_at": now}},
    )
    if updated.modified_count == 0:
        return
    thread = threading.Thread(target=run_time_tag_job, args=(job["job_id"], user), daemon=True)
    thread.start()


def run_time_tag_job(job_id: str, user: CurrentUser) -> None:
    try:
        while True:
            job = time_tag_job_collection().find_one({"job_id": job_id})
            if not job:
                return
            item = next((entry for entry in job.get("items") or [] if entry.get("status") in {"QUEUED", "FAILED"}), None)
            if not item:
                return
            process_time_tag_item(job_id, int(item.get("file_id") or 0), user)
    finally:
        time_tag_job_collection().update_one(
            {"job_id": job_id},
            {"$set": {"worker_running": False, "updated_at": datetime.utcnow()}},
        )
        refresh_time_tag_job_status(job_id)


def process_time_tag_item(job_id: str, file_id: int, user: CurrentUser) -> None:
    mark_time_tag_item(job_id, file_id, "RUNNING", "time tag analysis started")
    item = media_collection().find_one({"webhard_file_id": file_id}, media_list_projection())
    if not item:
        mark_time_tag_item(job_id, file_id, "FAILED", "media item not found")
        return
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    if karaoke_time_markers(tags):
        mark_time_tag_item(job_id, file_id, "SKIPPED_EXISTING", "time tags already exist")
        return
    try:
        youtube_url = youtube_url_from_media_item(item)
        if not youtube_url:
            mark_time_tag_item(job_id, file_id, "SKIPPED_MISSING_URL", "youtube url not found")
            return
        generated = youtube_time_tags(youtube_url, str(item.get("description") or ""))
        if not generated:
            storage_path = storage_path_from_media_item(user, item)
            if not storage_path:
                mark_time_tag_item(job_id, file_id, "SKIPPED_MISSING_FILE", "local video file not found")
                return
            generated = video_frame_time_tags(storage_path)
    except Exception as exc:
        LOGGER.warning("youtube time tag failed file_id=%s title=%s error=%s", file_id, media_item_log_label(item), str(exc)[:500])
        mark_time_tag_item(job_id, file_id, "FAILED", str(exc)[:500])
        return
    if not generated:
        mark_time_tag_item(job_id, file_id, "SKIPPED_EMPTY", "time tags were not detected")
        return
    next_tags = unique_tags(tags + generated)
    media_collection().update_one(
        {"webhard_file_id": file_id},
        {"$set": {"tags": next_tags, "updated_at": datetime.utcnow()}},
    )
    LOGGER.warning("youtube time tag updated file_id=%s tags=%s title=%s", file_id, generated, media_item_log_label(item))
    mark_time_tag_item(job_id, file_id, "UPDATED", "time tags generated", generated)


def mark_time_tag_item(job_id: str, file_id: int, status: str, message: str, tags: list[str] | None = None) -> None:
    now = datetime.utcnow()
    update = {
        "updated_at": now,
        "items.$.status": status,
        "items.$.message": message[:500],
    }
    if status == "RUNNING":
        update["items.$.started_at"] = now
        update["items.$.finished_at"] = None
    else:
        update["items.$.finished_at"] = now
    if tags is not None:
        update["items.$.tags"] = tags
    time_tag_job_collection().update_one(
        {"job_id": job_id, "items.file_id": file_id},
        {"$set": update, "$unset": {"items.$.worker_id": "", "items.$.lease_expires_at": ""}},
    )
    refresh_time_tag_job_status(job_id)


def refresh_time_tag_job_status(job_id: str) -> None:
    job = time_tag_job_collection().find_one({"job_id": job_id})
    if not job:
        return
    result = time_tag_job_result(job)
    running = sum(1 for item in job.get("items") or [] if item.get("status") == "RUNNING")
    queued = sum(1 for item in job.get("items") or [] if item.get("status") in {"QUEUED", "FAILED"})
    if running > 0:
        status = "RUNNING"
    elif queued > 0 and result["finished_count"] == 0:
        status = "QUEUED"
    elif queued > 0:
        status = "PARTIAL"
    elif result["failed_count"] > 0 and result["updated_count"] == 0:
        status = "FAILED"
    elif result["failed_count"] > 0:
        status = "PARTIAL"
    else:
        status = "DONE"
    time_tag_job_collection().update_one(
        {"job_id": job_id},
        {
            "$set": {
                "status": status,
                "message": time_tag_job_message(status, result),
                "checked_count": result["checked_count"],
                "updated_count": result["updated_count"],
                "failed_count": result["failed_count"],
                "skipped_existing_count": result["skipped_existing_count"],
                "skipped_empty_count": result["skipped_empty_count"],
                "skipped_missing_url_count": result["skipped_missing_url_count"],
                "skipped_missing_file_count": result["skipped_missing_file_count"],
                "worker_running": running > 0,
                "updated_at": datetime.utcnow(),
            }
        },
    )


def time_tag_job_message(status: str, result: dict[str, Any]) -> str:
    if status == "DONE":
        return "time tag generation completed"
    if status == "FAILED":
        return "time tag generation failed"
    return f"updated {result['updated_count']}, failed {result['failed_count']}, finished {result['finished_count']}/{result['item_count']}"


def serialize_time_tag_job(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = time_tag_job_result(job)
    active_item = next((serialize_time_tag_item(item) for item in job.get("items") or [] if item.get("status") == "RUNNING"), None)
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": job.get("message") or "",
        "item_count": result["item_count"],
        "checked_count": result["checked_count"],
        "updated_count": result["updated_count"],
        "failed_count": result["failed_count"],
        "skipped_existing_count": result["skipped_existing_count"],
        "skipped_empty_count": result["skipped_empty_count"],
        "skipped_missing_url_count": result["skipped_missing_url_count"],
        "skipped_missing_file_count": result["skipped_missing_file_count"],
        "finished_count": result["finished_count"],
        "progress_percent": result["progress_percent"],
        "worker_running": bool(job.get("worker_running")),
        "active_item": active_item,
        "items": [serialize_time_tag_item(item) for item in job.get("items") or []],
        "created_at": job.get("created_at").isoformat() if isinstance(job.get("created_at"), datetime) else "",
        "updated_at": job.get("updated_at").isoformat() if isinstance(job.get("updated_at"), datetime) else "",
    }


def serialize_time_tag_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_no": int(item.get("order_no") or 0),
        "file_id": int(item.get("file_id") or 0),
        "title": item.get("title") or "",
        "status": item.get("status") or "QUEUED",
        "message": item.get("message") or "",
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "started_at": item.get("started_at").isoformat() if isinstance(item.get("started_at"), datetime) else "",
        "finished_at": item.get("finished_at").isoformat() if isinstance(item.get("finished_at"), datetime) else "",
    }


def time_tag_job_result(job: dict[str, Any]) -> dict[str, Any]:
    items = job.get("items") or []
    updated = sum(1 for item in items if item.get("status") == "UPDATED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    existing = sum(1 for item in items if item.get("status") == "SKIPPED_EXISTING")
    empty = sum(1 for item in items if item.get("status") == "SKIPPED_EMPTY")
    missing_url = sum(1 for item in items if item.get("status") == "SKIPPED_MISSING_URL")
    missing_file = sum(1 for item in items if item.get("status") == "SKIPPED_MISSING_FILE")
    running = sum(1 for item in items if item.get("status") == "RUNNING")
    finished = updated + failed + existing + empty + missing_url + missing_file
    item_count = int(job.get("item_count") or len(items))
    return {
        "scanned_count": item_count,
        "checked_count": finished + running,
        "updated_count": updated,
        "failed_count": failed,
        "skipped_existing_count": existing,
        "skipped_empty_count": empty,
        "skipped_missing_url_count": missing_url,
        "skipped_missing_file_count": missing_file,
        "finished_count": finished,
        "item_count": item_count,
        "progress_percent": round((finished / item_count) * 100, 1) if item_count else 0,
        "results": [serialize_time_tag_item(item) for item in items],
    }


def youtube_job_collection():
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["youtube_import_jobs"]
    collection.create_index([("job_id", 1)], unique=True)
    collection.create_index([("owner_user_id", 1), ("updated_at", -1)])
    return collection


def create_youtube_import_job(url: str, user: CurrentUser, tags: list[str]) -> dict[str, Any]:
    preview = preview_youtube(url)
    job_id = uuid.uuid4().hex
    now = datetime.utcnow()
    items = []
    max_items = youtube_import_max_items()
    for index, item in enumerate((preview.get("items") or [])[:max_items]):
        video_id = str(item.get("youtube_video_id") or "").strip()
        if not is_safe_youtube_video_id(video_id):
            continue
        duplicate = existing_youtube_download(video_id, user)
        items.append({
            **item,
            "order_no": index + 1,
            "status": "SKIPPED_DUPLICATE" if duplicate else "QUEUED",
            "file_id": duplicate.get("webhard_file_id") if duplicate else None,
            "message": "already saved" if duplicate else "",
            "started_at": None,
            "finished_at": now if duplicate else None,
        })
    if not items:
        raise RuntimeError("youtube import items were not found")
    duplicate_count = sum(1 for item in items if item.get("status") == "SKIPPED_DUPLICATE")
    queued_count = sum(1 for item in items if item.get("status") == "QUEUED")
    doc = {
        "job_id": job_id,
        "owner_user_id": user.user_id,
        "owner_roles": user.roles,
        "owner_service_permissions": user.service_permissions,
        "owner_access_token": encrypt_job_secret(user.access_token),
        "status": "QUEUED" if queued_count else "DONE",
        "message": "youtube import job created" if queued_count else "youtube import completed",
        "url": url,
        "tags": tags,
        "playlist_id": preview.get("playlist_id") or "",
        "playlist_title": preview.get("playlist_title") or "",
        "title": preview.get("playlist_title") or preview.get("title") or "",
        "source_type": "YOUTUBE_DOWNLOAD",
        "items": items,
        "item_count": len(items),
        "source_item_count": int(preview.get("item_count") or len(items)),
        "truncated": int(preview.get("item_count") or len(items)) > len(items),
        "started_count": 0,
        "downloaded_count": 0,
        "skipped_duplicate_count": duplicate_count,
        "failed_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    youtube_job_collection().insert_one(doc)
    return serialize_youtube_job(doc)


def youtube_import_job(job_id: str, user: CurrentUser) -> dict[str, Any] | None:
    if not job_id:
        return None
    query = {"job_id": job_id}
    if not user.is_admin:
        query["owner_user_id"] = user.user_id
    job = youtube_job_collection().find_one(query)
    return serialize_youtube_job(job) if job else None


def start_youtube_import_item(job: dict[str, Any], video_id: str, user: CurrentUser) -> dict[str, Any]:
    if not is_safe_youtube_video_id(video_id):
        raise RuntimeError("youtube video id is invalid")
    target = next((item for item in job.get("items") or [] if str(item.get("youtube_video_id") or "") == video_id), None)
    if not target:
        raise RuntimeError("youtube import item not found")
    if target.get("status") in {"RUNNING", "SAVED", "SKIPPED_DUPLICATE"}:
        return job
    if youtube_running_count(job) >= YOUTUBE_IMPORT_CONCURRENCY:
        raise RuntimeError("youtube import concurrency limit exceeded")
    if not RUN_INLINE_JOBS:
        refresh_youtube_job_status(job["job_id"])
        return youtube_job_collection().find_one({"job_id": job["job_id"]}) or job
    mark_youtube_import_item_running(job["job_id"], video_id)
    thread = threading.Thread(target=run_youtube_import_item, args=(job["job_id"], video_id, user), daemon=True)
    thread.start()
    return youtube_job_collection().find_one({"job_id": job["job_id"]}) or job


def start_youtube_import_job(job: dict[str, Any], user: CurrentUser) -> int:
    if job.get("dispatcher_running"):
        return 0
    pending = [
        str(item.get("youtube_video_id") or "")
        for item in job.get("items") or []
        if item.get("status") in {"QUEUED", "FAILED"} and is_safe_youtube_video_id(str(item.get("youtube_video_id") or ""))
    ]
    if not pending:
        return 0
    now = datetime.utcnow()
    updated = youtube_job_collection().update_one(
        {"job_id": job["job_id"], "dispatcher_running": {"$ne": True}},
        {"$set": {"dispatcher_running": True, "status": "RUNNING", "message": "youtube import running", "updated_at": now}},
    )
    if updated.modified_count == 0:
        return 0
    if not RUN_INLINE_JOBS:
        return len(pending)
    thread = threading.Thread(target=run_youtube_import_job, args=(job["job_id"], pending, user), daemon=True)
    thread.start()
    return len(pending)


def run_youtube_import_job(job_id: str, pending_ids: list[str], user: CurrentUser) -> None:
    try:
        for video_id in pending_ids:
            job = youtube_job_collection().find_one({"job_id": job_id})
            if not job:
                return
            item = next((entry for entry in job.get("items") or [] if str(entry.get("youtube_video_id") or "") == video_id), None)
            if not item:
                continue
            if item.get("status") not in {"QUEUED", "FAILED"}:
                continue
            mark_youtube_import_item_running(job_id, video_id)
            run_youtube_import_item(job_id, video_id, user)
    finally:
        youtube_job_collection().update_one(
            {"job_id": job_id},
            {"$set": {"dispatcher_running": False, "updated_at": datetime.utcnow()}},
        )
        refresh_youtube_job_status(job_id)


def run_youtube_import_item(job_id: str, video_id: str, user: CurrentUser) -> None:
    with YOUTUBE_IMPORT_SEMAPHORE:
        job = youtube_job_collection().find_one({"job_id": job_id})
        if not job:
            return
        item = next((entry for entry in job.get("items") or [] if str(entry.get("youtube_video_id") or "") == video_id), None)
        if not item:
            return
        duplicate = existing_youtube_download(video_id, user)
        if duplicate:
            file_id = int(duplicate.get("webhard_file_id") or 0)
            update_youtube_import_item(job_id, video_id, "SKIPPED_DUPLICATE", "already saved", file_id, {
                "youtube_video_id": video_id,
                "file_id": file_id,
                "title": duplicate.get("title") or item.get("title") or video_id,
                "status": "SKIPPED_DUPLICATE",
            })
            return
        try:
            result = import_youtube_item(
                item,
                user,
                normalize_import_tags(job.get("tags") or []),
                str(job.get("playlist_id") or ""),
                str(job.get("playlist_title") or ""),
            )
            update_youtube_import_item(job_id, video_id, "SAVED", "saved", result.get("file_id"), result)
        except Exception as exc:
            update_youtube_import_item(job_id, video_id, "FAILED", str(exc)[:500], None, None)


def mark_youtube_import_item_running(job_id: str, video_id: str) -> None:
    now = datetime.utcnow()
    youtube_job_collection().update_one(
        {"job_id": job_id, "items.youtube_video_id": video_id},
        {
            "$set": {
                "status": "RUNNING",
                "message": "youtube import running",
                "updated_at": now,
                "items.$.status": "RUNNING",
                "items.$.message": "download started",
                "items.$.started_at": now,
                "items.$.finished_at": None,
            }
        },
    )


def update_youtube_import_item(
    job_id: str,
    video_id: str,
    status: str,
    message: str,
    file_id: int | None,
    result: dict[str, Any] | None,
) -> None:
    now = datetime.utcnow()
    update = {
        "updated_at": now,
        "items.$.status": status,
        "items.$.message": message,
        "items.$.file_id": file_id,
        "items.$.finished_at": now,
    }
    if result:
        update["items.$.result"] = result
    youtube_job_collection().update_one(
        {"job_id": job_id, "items.youtube_video_id": video_id},
        {"$set": update, "$unset": {"items.$.worker_id": "", "items.$.lease_expires_at": ""}},
    )
    refresh_youtube_job_status(job_id)


def refresh_youtube_job_status(job_id: str) -> None:
    job = youtube_job_collection().find_one({"job_id": job_id})
    if not job:
        return
    items = job.get("items") or []
    saved = sum(1 for item in items if item.get("status") == "SAVED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    duplicate = sum(1 for item in items if item.get("status") == "SKIPPED_DUPLICATE")
    running = sum(1 for item in items if item.get("status") == "RUNNING")
    queued = sum(1 for item in items if item.get("status") == "QUEUED")
    if running > 0:
        status = "RUNNING"
    elif queued > 0 and saved + failed + duplicate == 0:
        status = "QUEUED"
    elif queued > 0:
        status = "PARTIAL"
    elif failed > 0 and saved == 0:
        status = "FAILED"
    elif failed > 0:
        status = "PARTIAL"
    else:
        status = "DONE"
    youtube_job_collection().update_one(
        {"job_id": job_id},
        {
            "$set": {
                "status": status,
                "message": youtube_job_message(status, saved, failed, queued, running),
                "downloaded_count": saved,
                "failed_count": failed,
                "skipped_duplicate_count": duplicate,
                "started_count": saved + failed + duplicate + running,
                "dispatcher_running": running > 0,
                "updated_at": datetime.utcnow(),
            }
        },
    )


def youtube_job_message(status: str, saved: int, failed: int, queued: int, running: int) -> str:
    if status == "DONE":
        return "youtube import completed"
    if status == "FAILED":
        return "youtube import failed"
    return f"saved {saved}, failed {failed}, running {running}, queued {queued}"


def serialize_youtube_job(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    items = [serialize_youtube_job_item(item) for item in job.get("items") or []]
    item_count = int(job.get("item_count") or len(items))
    downloaded_count = sum(1 for item in items if item.get("status") == "SAVED")
    failed_count = sum(1 for item in items if item.get("status") == "FAILED")
    duplicate_count = sum(1 for item in items if item.get("status") == "SKIPPED_DUPLICATE")
    running_count = sum(1 for item in items if item.get("status") == "RUNNING")
    queued_count = sum(1 for item in items if item.get("status") == "QUEUED")
    finished_count = downloaded_count + failed_count + duplicate_count
    active_item = next((item for item in items if item.get("status") == "RUNNING"), None)
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": job.get("message"),
        "title": job.get("title") or "",
        "playlist_id": job.get("playlist_id") or "",
        "playlist_title": job.get("playlist_title") or "",
        "item_count": item_count,
        "source_item_count": int(job.get("source_item_count") or item_count),
        "truncated": bool(job.get("truncated")),
        "dispatcher_running": bool(job.get("dispatcher_running")),
        "downloaded_count": downloaded_count,
        "failed_count": failed_count,
        "skipped_duplicate_count": duplicate_count,
        "running_count": running_count,
        "queued_count": queued_count,
        "finished_count": finished_count,
        "progress_percent": round((finished_count / item_count) * 100, 1) if item_count else 0,
        "active_item": active_item,
        "items": items,
        "result": youtube_job_result(job, items),
        "created_at": job.get("created_at").isoformat() if isinstance(job.get("created_at"), datetime) else "",
        "updated_at": job.get("updated_at").isoformat() if isinstance(job.get("updated_at"), datetime) else "",
    }


def serialize_youtube_job_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_no": int(item.get("order_no") or 0),
        "youtube_video_id": item.get("youtube_video_id") or "",
        "title": item.get("title") or item.get("youtube_video_id") or "",
        "thumbnail_url": item.get("thumbnail_url") or "",
        "duration": item.get("duration"),
        "channel_name": item.get("channel_name") or "",
        "status": item.get("status") or "QUEUED",
        "file_id": item.get("file_id"),
        "webhard_file_id": item.get("file_id"),
        "message": item.get("message") or "",
        "started_at": item.get("started_at").isoformat() if isinstance(item.get("started_at"), datetime) else "",
        "finished_at": item.get("finished_at").isoformat() if isinstance(item.get("finished_at"), datetime) else "",
    }


def youtube_job_result(job: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for item in items:
        if item.get("status") == "SAVED":
            results.append({
                "youtube_video_id": item.get("youtube_video_id"),
                "file_id": item.get("file_id"),
                "title": item.get("title"),
                "status": "DOWNLOADED",
            })
        elif item.get("status") == "SKIPPED_DUPLICATE":
            results.append({
                "youtube_video_id": item.get("youtube_video_id"),
                "file_id": item.get("file_id"),
                "title": item.get("title"),
                "status": "SKIPPED_DUPLICATE",
            })
        elif item.get("status") == "FAILED":
            results.append({
                "youtube_video_id": item.get("youtube_video_id"),
                "title": item.get("title"),
                "status": "FAILED",
                "message": item.get("message") or "저장 실패",
            })
    downloaded = sum(1 for item in items if item.get("status") == "SAVED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    duplicate = sum(1 for item in items if item.get("status") == "SKIPPED_DUPLICATE")
    return {
        "source_type": "YOUTUBE_DOWNLOAD",
        "scanned_count": int(job.get("item_count") or len(items)),
        "downloaded_count": downloaded,
        "upserted_count": downloaded,
        "skipped_count": duplicate,
        "skipped_duplicate_count": duplicate,
        "failed_count": failed,
        "results": results,
    }


def existing_youtube_download(video_id: str, user: CurrentUser) -> dict[str, Any] | None:
    query: dict[str, Any] = {
        "source_type": "YOUTUBE_DOWNLOAD",
        "youtube_video_id": video_id,
    }
    if not user.is_admin:
        query["owner_user_id"] = user.user_id
    return media_collection().find_one(query, {"webhard_file_id": 1, "title": 1})


def can_manage_media(user: CurrentUser, item: dict[str, Any]) -> bool:
    return user.is_admin or str(item.get("owner_user_id") or "") == user.user_id


def channel_name(item: dict[str, Any]) -> str:
    owner = str(item.get("owner_user_id") or "creator").strip()
    return f"{owner} 채널"


def karaoke_number(item: dict[str, Any]) -> str:
    values = list(item.get("tags") or [])
    values.extend(item.get("webhard_tags") or [])
    values.extend([item.get("title"), item.get("display_name"), item.get("file_name")])
    for value in values:
        match = re.search(r"KY\.?(\d{3,7})", str(value or ""), flags=re.IGNORECASE)
        if match:
            return f"KY.{match.group(1)}"
        numeric_match = re.fullmatch(r"\d{3,7}", str(value or "").strip())
        if numeric_match:
            return f"KY.{numeric_match.group(0)}"
    return ""


def karaoke_artist(item: dict[str, Any]) -> str:
    if item.get("channel_name"):
        return str(item.get("channel_name"))
    if item.get("album"):
        return str(item.get("album"))
    for tag in list(item.get("tags") or []) + list(item.get("webhard_tags") or []):
        text = str(tag or "").strip()
        if text and not re.match(r"KY\.?\d+", text, flags=re.IGNORECASE) and not re.fullmatch(r"\d{3,7}", text) and not parse_time_marker(text):
            return text
    return ""


def karaoke_time_markers(tags: list[Any]) -> list[dict[str, Any]]:
    markers = []
    seen = set()
    for tag in tags:
        marker = parse_time_marker(str(tag or ""))
        if not marker:
            continue
        key = marker["seconds"]
        if key in seen:
            continue
        seen.add(key)
        markers.append(marker)
    markers.sort(key=lambda item: item["seconds"])
    return markers


def parse_time_marker(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    match = re.search(r"(?:^|[^\d])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d(?:\.\d{1,3})?)(?!\d)", text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    total_seconds = round(hours * 3600 + minutes * 60 + seconds, 3)
    label = re.sub(re.escape(match.group(0)), " ", text, count=1).strip() or text
    return {"seconds": total_seconds, "label": label, "raw": text}


def youtube_url_from_media_item(item: dict[str, Any]) -> str:
    youtube_url = str(item.get("youtube_url") or "").strip()
    if youtube_url:
        return youtube_url
    video_id = str(item.get("youtube_video_id") or "").strip()
    if not video_id:
        candidates = [
            item.get("file_name"),
            item.get("display_name"),
            item.get("title"),
        ]
        for candidate in candidates:
            video_id = youtube_video_id_from_text(str(candidate or ""))
            if video_id:
                break
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_video_id_from_text(value: str) -> str:
    text = str(value or "")
    bracket_match = re.search(r"\[([A-Za-z0-9_-]{11})\](?:\.[A-Za-z0-9]+)?$", text)
    if bracket_match:
        return bracket_match.group(1)
    url_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", text)
    if url_match:
        return url_match.group(1)
    return ""


def storage_path_from_media_item(user: CurrentUser, item: dict[str, Any]) -> str:
    storage_path = str(item.get("storage_path") or "").strip()
    if storage_path:
        return storage_path
    file_id = int(item.get("webhard_file_id") or 0)
    if file_id <= 0:
        return ""
    try:
        detail = fetch_webhard_file(user, file_id, allow_public=True) or {}
    except Exception as exc:
        LOGGER.warning("youtube time tag webhard detail failed file_id=%s error=%s", file_id, str(exc)[:300])
        return ""
    storage_path = str(detail.get("storage_path") or "").strip()
    if storage_path:
        media_collection().update_one({"webhard_file_id": file_id}, {"$set": {"storage_path": storage_path}})
    return storage_path


def media_item_log_label(item: dict[str, Any]) -> str:
    label = str(item.get("title") or item.get("display_name") or item.get("file_name") or "").strip()
    return label[:120]


def git_commit() -> str:
    env_commit = os.getenv("GIT_COMMIT") or os.getenv("VITE_GIT_COMMIT")
    if env_commit:
        return env_commit[:12]

    repo_dir = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def int_param(request: HttpRequest, name: str, default: int) -> int:
    try:
        return int(request.GET.get(name) or default)
    except ValueError:
        return default


def karaoke_search_terms(keyword: str) -> list[str]:
    text = str(keyword or "").strip()
    terms = [text] if text else []
    match = re.fullmatch(r"(?:KY\.?)?(\d{1,7})", text, flags=re.IGNORECASE)
    if match:
        number = match.group(1)
        padded_numbers = [number]
        for width in (4, 5, 6, 7):
            if len(number) < width:
                padded_numbers.append(number.zfill(width))
        for value in padded_numbers:
            terms.extend([value, f"KY.{value}", f"KY{value}"])
    result = []
    for term in terms:
        if term and term not in result:
            result.append(term)
    return result


def media_search_query(fields: list[str], keyword: str, include_karaoke_number_terms: bool = False) -> dict[str, Any]:
    phrase_terms = karaoke_search_terms(keyword) if include_karaoke_number_terms else unique_search_terms([keyword])
    phrase_conditions = [
        {field_name: {"$regex": re.escape(term), "$options": "i"}}
        for field_name in fields
        for term in phrase_terms
    ]
    token_groups = [
        {
            "$or": [
                {field_name: {"$regex": re.escape(token), "$options": "i"}}
                for field_name in fields
            ]
        }
        for token in search_tokens(keyword)
    ]
    if token_groups:
        return {"$or": phrase_conditions + [{"$and": token_groups}]}
    return {"$or": phrase_conditions}


def search_tokens(keyword: str) -> list[str]:
    text = str(keyword or "").strip()
    raw_tokens = re.split(r"[\s\[\]\(\)\{\}_.,\-+~!@#$%^&=;:'\"\\/|]+", text)
    return unique_search_terms(token for token in raw_tokens if len(token) >= 2 or token.isdigit())[:8]


def unique_search_terms(terms) -> list[str]:
    result = []
    for term in terms:
        text = str(term or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def is_remote_rate_limited(session: dict[str, Any]) -> bool:
    now = datetime.utcnow()
    recent_count = 0
    for command in session.get("commands") or []:
        created_at = command.get("created_at")
        if isinstance(created_at, datetime) and (now - created_at).total_seconds() <= 10:
            recent_count += 1
    return recent_count >= 20


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def redact_sensitive_text(value: Any) -> str:
    return SENSITIVE_PATH_RE.sub(r"\1***", str(value or ""))


def pairing_attempt_keys(request: HttpRequest, pairing_code: str, pairing_token: str) -> list[str]:
    keys = [f"ip:{client_ip(request)}"]
    if pairing_code:
        keys.append(f"code:{pairing_code}")
    if pairing_token:
        keys.append(f"token:{token_hash(pairing_token)}")
    return keys


def is_pairing_rate_limited(keys: list[str], now: datetime) -> bool:
    if not keys:
        return False
    since = now - timedelta(seconds=PAIR_ATTEMPT_WINDOW_SECONDS)
    collection = karaoke_pair_attempt_collection()
    for key in keys:
        limit = PAIR_CODE_MAX_FAILURES if key.startswith("code:") else PAIR_IP_MAX_FAILURES
        count = collection.count_documents({"key": key, "created_at": {"$gte": since}}, limit=limit)
        if count >= limit:
            return True
    return False


def record_pairing_failure(keys: list[str], now: datetime) -> None:
    if not keys:
        return
    docs = [{"key": key, "created_at": now} for key in keys]
    karaoke_pair_attempt_collection().insert_many(docs, ordered=False)


def client_ip(request: HttpRequest) -> str:
    return str(request.META.get("REMOTE_ADDR") or "unknown").strip() or "unknown"


def qr_svg_data_url(value: str) -> str:
    image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage)
    svg = image.to_string(encoding="unicode")
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def frontend_base_url(request: HttpRequest) -> str:
    configured = str(settings.MEDIA_CONFIG.get("MEDIA_QR_BASE_URL") or settings.MEDIA_CONFIG.get("MEDIA_FRONTEND_BASE_URL") or "").rstrip("/")
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    allowed = {configured, *[item.rstrip("/") for item in getattr(settings, "CORS_ORIGINS", [])]}
    if origin and origin in allowed:
        return origin
    return configured


def tv_session_by_token(tv_token: str) -> dict[str, Any] | None:
    token = str(tv_token or "").strip()
    if not token:
        return None
    now = datetime.utcnow()
    collection = karaoke_remote_collection()
    session = collection.find_one({
        "session_type": "TV",
        "tv_token_hash": token_hash(token),
        "expires_at": {"$gt": now},
    })
    if not session:
        return None
    if tv_session_is_stale(session, now):
        expire_tv_session(session, now)
        return None
    patch = {"updated_at": now, "last_tv_seen_at": now}
    if session.get("status") == "PAIRED":
        patch["expires_at"] = now + timedelta(hours=12)
    collection.update_one({"_id": session["_id"]}, {"$set": patch})
    session.update(patch)
    return session


def tv_session_is_stale(session: dict[str, Any], now: datetime) -> bool:
    if session.get("session_type") != "TV" or session.get("status") != "PAIRED":
        return False
    last_seen = session.get("last_tv_seen_at") or session.get("updated_at") or session.get("paired_at")
    if not isinstance(last_seen, datetime):
        return False
    return (now - last_seen).total_seconds() > TV_SIGNAL_TIMEOUT_SECONDS


def expire_tv_session(session: dict[str, Any], now: datetime) -> None:
    if not session.get("_id"):
        return
    karaoke_remote_collection().update_one(
        {"_id": session["_id"]},
        {"$set": {"status": "EXPIRED", "updated_at": now, "expires_at": now}},
    )


def tv_media_file_allowed(session: dict[str, Any], webhard_file_id: int) -> bool:
    try:
        target_id = int(webhard_file_id)
    except (TypeError, ValueError):
        return False
    queue_state = account_queue_state(session)
    candidates = []
    current_item = queue_state.get("current_item")
    if isinstance(current_item, dict):
        candidates.append(current_item)
    candidates.extend(item for item in queue_state.get("queue") or [] if isinstance(item, dict))
    for item in candidates:
        try:
            if int(item.get("webhard_file_id") or 0) == target_id:
                return True
        except (TypeError, ValueError):
            continue
    return False


def remote_session_role(session: dict[str, Any], user: CurrentUser) -> str:
    if str(session.get("owner_user_id") or "") == user.user_id:
        return "HOST"
    if session.get("session_type") == "TV":
        for participant in session.get("participants") or []:
            if isinstance(participant, dict) and str(participant.get("user_id") or "") == user.user_id:
                return "GUEST"
    return ""


def remote_payload_user(session: dict[str, Any], user: CurrentUser) -> CurrentUser:
    if session.get("session_type") != "TV" or not session.get("owner_user_id"):
        return user
    return CurrentUser(
        user_id=str(session.get("owner_user_id") or ""),
        roles=["ROLE_ADMIN"] if session.get("owner_is_admin") else [],
        service_permissions={"MEDIA_SERVICE": ["READ"]},
    )


def remote_reserver_label(role: str, user: CurrentUser) -> str:
    return str(user.user_id or "").strip()[:40]


def serialize_tv_session(session: dict[str, Any]) -> dict[str, Any]:
    queue_state = account_queue_state(session)
    current_item = queue_state.get("current_item") if isinstance(queue_state.get("current_item"), dict) else None
    return {
        "session_id": session.get("session_id") or "",
        "status": session.get("status") or "WAITING",
        "paired": session.get("status") == "PAIRED" and bool(session.get("owner_user_id")),
        "owner_user_id": session.get("owner_user_id") or "",
        "pairing_code": session.get("pairing_code") or "",
        "queue": [serialize_remote_media_item(item) for item in queue_state.get("queue") or [] if isinstance(item, dict)],
        "current_item": serialize_remote_media_item(current_item) if current_item else None,
        "playback_state": queue_state.get("playback_state") or "IDLE",
        "expires_at": iso_datetime(session.get("expires_at")),
        "paired_at": iso_datetime(session.get("paired_at")),
    }


def ensure_account_queue(owner_user_id: str, now: datetime) -> None:
    owner = str(owner_user_id or "").strip()
    if not owner:
        return
    karaoke_queue_collection().update_one(
        {"owner_user_id": owner},
        {
            "$setOnInsert": {
                "owner_user_id": owner,
                "queue": [],
                "current_item": None,
                "playback_state": "IDLE",
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
    )


def account_queue_state(session: dict[str, Any]) -> dict[str, Any]:
    owner_user_id = str(session.get("owner_user_id") or "").strip()
    if owner_user_id:
        doc = karaoke_queue_collection().find_one({"owner_user_id": owner_user_id})
        if doc:
            return {
                "queue": doc.get("queue") or [],
                "current_item": doc.get("current_item") if isinstance(doc.get("current_item"), dict) else None,
                "playback_state": doc.get("playback_state") or "IDLE",
            }
    return {
        "queue": session.get("queue") or [],
        "current_item": session.get("current_item") if isinstance(session.get("current_item"), dict) else None,
        "playback_state": session.get("playback_state") or "IDLE",
    }


def save_account_queue_state(session: dict[str, Any], state_patch: dict[str, Any], now: datetime) -> None:
    owner_user_id = str(session.get("owner_user_id") or "").strip()
    if not owner_user_id:
        return
    karaoke_queue_collection().update_one(
        {"owner_user_id": owner_user_id},
        {
            "$set": {
                "queue": state_patch.get("queue") or [],
                "current_item": state_patch.get("current_item") if isinstance(state_patch.get("current_item"), dict) else None,
                "playback_state": state_patch.get("playback_state") or "IDLE",
                "updated_at": now,
            },
            "$setOnInsert": {"owner_user_id": owner_user_id, "created_at": now},
        },
        upsert=True,
    )


def remote_state_patch(session: dict[str, Any], command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    queue = [item for item in session.get("queue") or [] if isinstance(item, dict) and item.get("webhard_file_id")]
    current_item = session.get("current_item") if isinstance(session.get("current_item"), dict) else None
    item = payload.get("item") if isinstance(payload.get("item"), dict) else None
    playback_state = str(session.get("playback_state") or "IDLE")

    if command_type == "PLAY_ITEM" and item:
        current_item = item
        playback_state = "PLAYING"
    elif command_type == "RESERVE_ITEM" and item:
        item_id = int(item.get("webhard_file_id") or 0)
        if item_id and all(int(entry.get("webhard_file_id") or 0) != item_id for entry in queue):
            queue = (queue + [item])[:50]
    elif command_type == "NEXT":
        if queue:
            current_item = queue[0]
            queue = queue[1:]
            playback_state = "PLAYING"
        else:
            current_item = None
            playback_state = "IDLE"
    elif command_type == "CLEAR_QUEUE":
        queue = []
    elif command_type == "TOGGLE_PLAY":
        playback_state = "PAUSED" if playback_state == "PLAYING" else "PLAYING"

    return {
        "queue": queue,
        "current_item": current_item,
        "playback_state": playback_state,
    }


def queue_has_media_item(session: dict[str, Any], webhard_file_id: int) -> bool:
    if not webhard_file_id:
        return False
    current_item = session.get("current_item") if isinstance(session.get("current_item"), dict) else None
    if current_item and media_item_id(current_item) == webhard_file_id:
        return True
    return any(
        media_item_id(item) == webhard_file_id
        for item in session.get("queue") or []
        if isinstance(item, dict)
    )


def media_item_id(item: dict[str, Any]) -> int:
    try:
        return int(item.get("webhard_file_id") or 0)
    except (TypeError, ValueError):
        return 0


def serialize_remote_media_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "webhard_file_id",
        "title",
        "display_name",
        "file_name",
        "content_kind",
        "content_type",
        "thumbnail_url",
        "tags",
        "webhard_tags",
        "album",
        "channel_name",
        "karaoke_number",
        "karaoke_artist",
        "time_markers",
        "reserved_by",
        "reserved_by_user_id",
        "reserved_by_role",
    }
    return {key: item[key] for key in allowed if key in item}


def sanitize_remote_payload(user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else None
    result: dict[str, Any] = {}
    if item:
        try:
            webhard_file_id = int(item.get("webhard_file_id") or 0)
        except (TypeError, ValueError):
            webhard_file_id = 0
        media_item = media_collection().find_one(readable_media_query(user, webhard_file_id)) if webhard_file_id else None
        if media_item:
            result["item"] = serialize_media(media_item)
    return result


def iso_datetime(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def serialize_remote_command(command: dict[str, Any]) -> dict[str, Any]:
    result = dict(command)
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
    return result


def serialize_tv_command(command: dict[str, Any]) -> dict[str, Any]:
    result = serialize_remote_command(command)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    item = payload.get("item") if isinstance(payload.get("item"), dict) else None
    if item:
        result["payload"] = {"item": serialize_remote_media_item(item)}
    elif payload:
        result["payload"] = payload
    else:
        result["payload"] = {}
    return result


def optional_seconds(value: Any) -> float | bool | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    if not 0 <= parsed <= 24 * 60 * 60:
        return False
    return parsed


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def normalize_tags(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").split(",")
    result = []
    for item in items:
        tag = str(item).strip()
        if tag and tag not in result:
            result.append(tag[:40])
    return result[:30]


def unique_tags(values: list[Any]) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text[:40])
    return result[:30]


def normalize_import_tags(value: Any) -> list[str]:
    tags = ["youtube"]
    for tag in normalize_tags(value):
        if tag not in tags:
            tags.append(tag)
    return tags[:30]


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    allowed_hosts = {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed_hosts


def is_safe_youtube_video_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,80}", str(value or "").strip()))


def youtube_import_max_items() -> int:
    try:
        return min(max(int(settings.MEDIA_CONFIG.get("YOUTUBE_IMPORT_MAX_ITEMS") or 100), 1), 500)
    except (TypeError, ValueError):
        return 100


def youtube_running_count(job: dict[str, Any]) -> int:
    return sum(1 for item in job.get("items") or [] if item.get("status") == "RUNNING")
