import os
import socket
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings

from .mongo import mongo_client


def worker_heartbeat_collection():
    collection = media_db()["media_worker_heartbeats"]
    collection.create_index([("worker_id", 1)], unique=True)
    collection.create_index([("heartbeat_at", 1)])
    return collection


def youtube_video_lock_collection():
    collection = media_db()["youtube_video_locks"]
    collection.create_index([("video_id", 1)], unique=True)
    collection.create_index([("lease_expires_at", 1)])
    return collection


def media_db():
    return mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]


def default_worker_id() -> str:
    return os.getenv("MEDIA_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def update_worker_heartbeat(
    worker_id: str,
    queues: list[str],
    status: str = "IDLE",
    active: dict[str, Any] | None = None,
    message: str = "",
) -> None:
    now = datetime.utcnow()
    active = active or {}
    worker_heartbeat_collection().update_one(
        {"worker_id": worker_id},
        {
            "$set": {
                "worker_id": worker_id,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "queues": queues,
                "status": status,
                "message": message[:500],
                "active_job_type": active.get("job_type"),
                "active_job_id": active.get("job_id"),
                "active_item_id": active.get("item_id"),
                "active_video_id": active.get("video_id"),
                "active_file_id": active.get("file_id"),
                "heartbeat_at": now,
                "git_commit": os.getenv("GIT_COMMIT") or os.getenv("JSTUBE_WORKER_GIT_COMMIT") or "unknown",
            },
            "$setOnInsert": {"started_at": now},
        },
        upsert=True,
    )


def worker_status_snapshot() -> dict[str, Any]:
    now = datetime.utcnow()
    stale_seconds = int(os.getenv("MEDIA_WORKER_STALE_SECONDS", "60"))
    expected = int(os.getenv("MEDIA_WORKER_EXPECTED_REPLICAS", os.getenv("JSTUBE_WORKER_REPLICAS", "3")))
    stale_after = now - timedelta(seconds=max(stale_seconds, 10))

    pods = []
    active_count = 0
    for row in worker_heartbeat_collection().find({}, {"_id": 0}).sort("worker_id", 1).limit(50):
        heartbeat_at = row.get("heartbeat_at")
        stale = not isinstance(heartbeat_at, datetime) or heartbeat_at < stale_after
        if not stale:
            active_count += 1
        row["stale"] = stale
        pods.append(serialize_dates(row))

    locks = [
        serialize_dates(row)
        for row in youtube_video_lock_collection().find({}, {"_id": 0}).sort("updated_at", -1).limit(30)
    ]
    status = "UP" if active_count >= expected else ("DEGRADED" if active_count > 0 else "DOWN")
    return {
        "status": status,
        "checked_at": now.isoformat() + "Z",
        "expected_replicas": expected,
        "active_count": active_count,
        "stale_seconds": stale_seconds,
        "pods": pods,
        "locks": locks,
        "jobs": {
            "youtube": job_item_status_counts("youtube_import_jobs"),
            "time_tags": job_item_status_counts("youtube_time_tag_jobs"),
        },
    }


def job_item_status_counts(collection_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    pipeline = [
        {"$unwind": "$items"},
        {"$group": {"_id": "$items.status", "count": {"$sum": 1}}},
    ]
    for row in media_db()[collection_name].aggregate(pipeline):
        counts[str(row.get("_id") or "UNKNOWN")] = int(row.get("count") or 0)
    return counts


def serialize_dates(value):
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    if isinstance(value, dict):
        return {key: serialize_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_dates(item) for item in value]
    return value
