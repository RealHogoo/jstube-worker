import os
import socket
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument

from .auth import CurrentUser
from .job_crypto import decrypt_job_secret
from .views import (
    import_youtube_item,
    normalize_import_tags,
    process_time_tag_item,
    refresh_time_tag_job_status,
    refresh_youtube_job_status,
    time_tag_job_collection,
    update_youtube_import_item,
    youtube_job_collection,
)


YOUTUBE_STALE_STATUSES = {"QUEUED", "FAILED"}
TIME_TAG_STALE_STATUSES = {"QUEUED", "FAILED"}


def worker_id() -> str:
    return os.getenv("MEDIA_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def claim_youtube_item(worker: str, lease_seconds: int) -> dict[str, Any] | None:
    now = datetime.utcnow()
    reset_stale_youtube_items(now)
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    job = youtube_job_collection().find_one_and_update(
        {"items": {"$elemMatch": {"status": {"$in": sorted(YOUTUBE_STALE_STATUSES)}}}},
        {
            "$set": {
                "status": "RUNNING",
                "message": "youtube import running",
                "dispatcher_running": True,
                "updated_at": now,
                "items.$.status": "RUNNING",
                "items.$.message": "worker started",
                "items.$.started_at": now,
                "items.$.finished_at": None,
                "items.$.worker_id": worker,
                "items.$.lease_expires_at": lease_expires_at,
            }
        },
        sort=[("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return None
    item = next((entry for entry in job.get("items") or [] if entry.get("worker_id") == worker and entry.get("status") == "RUNNING"), None)
    if not item:
        return None
    return {"job": job, "item": item}


def run_claimed_youtube_item(claim: dict[str, Any]) -> str:
    job = claim["job"]
    item = claim["item"]
    video_id = str(item.get("youtube_video_id") or "")
    user = user_from_job(job)
    try:
        result = import_youtube_item(
            item,
            user,
            normalize_import_tags(job.get("tags") or []),
            str(job.get("playlist_id") or ""),
            str(job.get("playlist_title") or ""),
        )
        update_youtube_import_item(job["job_id"], video_id, "SAVED", "saved", result.get("file_id"), result)
        return "SAVED"
    except Exception as exc:
        update_youtube_import_item(job["job_id"], video_id, "FAILED", str(exc)[:500], None, None)
        return "FAILED"


def reset_stale_youtube_items(now: datetime) -> None:
    while True:
        result = youtube_job_collection().update_one(
            {
                "items": {
                    "$elemMatch": {
                        "status": "RUNNING",
                        "lease_expires_at": {"$lt": now},
                    }
                }
            },
            {
                "$set": {
                    "updated_at": now,
                    "items.$.status": "FAILED",
                    "items.$.message": "worker lease expired",
                    "items.$.finished_at": now,
                },
                "$unset": {
                    "items.$.worker_id": "",
                    "items.$.lease_expires_at": "",
                },
            },
        )
        if result.modified_count == 0:
            break


def claim_time_tag_item(worker: str, lease_seconds: int) -> dict[str, Any] | None:
    now = datetime.utcnow()
    reset_stale_time_tag_items(now)
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    job = time_tag_job_collection().find_one_and_update(
        {"items": {"$elemMatch": {"status": {"$in": sorted(TIME_TAG_STALE_STATUSES)}}}},
        {
            "$set": {
                "status": "RUNNING",
                "message": "time tag generation running",
                "worker_running": True,
                "updated_at": now,
                "items.$.status": "RUNNING",
                "items.$.message": "worker started",
                "items.$.started_at": now,
                "items.$.finished_at": None,
                "items.$.worker_id": worker,
                "items.$.lease_expires_at": lease_expires_at,
            }
        },
        sort=[("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return None
    item = next((entry for entry in job.get("items") or [] if entry.get("worker_id") == worker and entry.get("status") == "RUNNING"), None)
    if not item:
        return None
    return {"job": job, "item": item}


def run_claimed_time_tag_item(claim: dict[str, Any]) -> str:
    job = claim["job"]
    item = claim["item"]
    user = user_from_job(job)
    process_time_tag_item(str(job.get("job_id") or ""), int(item.get("file_id") or 0), user)
    refresh_time_tag_job_status(str(job.get("job_id") or ""))
    return "DONE"


def reset_stale_time_tag_items(now: datetime) -> None:
    while True:
        result = time_tag_job_collection().update_one(
            {
                "items": {
                    "$elemMatch": {
                        "status": "RUNNING",
                        "lease_expires_at": {"$lt": now},
                    }
                }
            },
            {
                "$set": {
                    "updated_at": now,
                    "items.$.status": "FAILED",
                    "items.$.message": "worker lease expired",
                    "items.$.finished_at": now,
                },
                "$unset": {
                    "items.$.worker_id": "",
                    "items.$.lease_expires_at": "",
                },
            },
        )
        if result.modified_count == 0:
            break


def user_from_job(job: dict[str, Any]) -> CurrentUser:
    roles = job.get("owner_roles")
    permissions = job.get("owner_service_permissions")
    return CurrentUser(
        user_id=str(job.get("owner_user_id") or ""),
        roles=roles if isinstance(roles, list) else ["ROLE_ADMIN"],
        service_permissions=permissions if isinstance(permissions, dict) else {},
        access_token=decrypt_job_secret(str(job.get("owner_access_token") or "")),
    )


def refresh_worker_job_statuses() -> None:
    for job in youtube_job_collection().find({"status": "RUNNING"}, {"job_id": 1}).limit(200):
        refresh_youtube_job_status(str(job.get("job_id") or ""))
    for job in time_tag_job_collection().find({"status": "RUNNING"}, {"job_id": 1}).limit(200):
        refresh_time_tag_job_status(str(job.get("job_id") or ""))
