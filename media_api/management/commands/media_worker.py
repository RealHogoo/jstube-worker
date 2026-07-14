import os
import time

from django.core.management.base import BaseCommand

from media_api.job_worker import (
    claim_time_tag_item,
    claim_youtube_item,
    refresh_worker_job_statuses,
    run_claimed_time_tag_item,
    run_claimed_youtube_item,
    worker_id,
)
from media_api.worker_status import update_worker_heartbeat


class Command(BaseCommand):
    help = "Run media background jobs for youtube import and time tag generation."

    def add_arguments(self, parser):
        parser.add_argument("--sleep", type=float, default=float(os.getenv("MEDIA_WORKER_POLL_SECONDS", "2")))
        parser.add_argument("--lease-seconds", type=int, default=int(os.getenv("MEDIA_WORKER_LEASE_SECONDS", "7200")))
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        sleep_seconds = max(float(options["sleep"]), 0.2)
        lease_seconds = max(int(options["lease_seconds"]), 60)
        once = bool(options["once"])
        queues = {
            item.strip().lower()
            for item in os.getenv("MEDIA_WORKER_QUEUES", "youtube,time-tags").split(",")
            if item.strip()
        }
        current_worker = worker_id()
        queue_list = sorted(queues)
        update_worker_heartbeat(current_worker, queue_list, "IDLE", message="worker started")
        self.stdout.write(self.style.SUCCESS(f"media worker started id={current_worker} queues={','.join(queue_list)}"))
        while True:
            did_work = False
            if "youtube" in queues:
                claim = claim_youtube_item(current_worker, lease_seconds)
                if claim:
                    item = claim["item"]
                    update_worker_heartbeat(
                        current_worker,
                        queue_list,
                        "RUNNING",
                        {
                            "job_type": "youtube",
                            "job_id": claim["job"].get("job_id"),
                            "video_id": item.get("youtube_video_id"),
                        },
                        "youtube import running",
                    )
                    self.stdout.write(f"youtube job={claim['job'].get('job_id')} video={item.get('youtube_video_id')} started")
                    status = run_claimed_youtube_item(claim)
                    update_worker_heartbeat(current_worker, queue_list, "IDLE", message=f"youtube item {status}")
                    self.stdout.write(f"youtube job={claim['job'].get('job_id')} video={item.get('youtube_video_id')} {status}")
                    did_work = True
            if "time-tags" in queues or "time_tags" in queues:
                claim = claim_time_tag_item(current_worker, lease_seconds)
                if claim:
                    item = claim["item"]
                    update_worker_heartbeat(
                        current_worker,
                        queue_list,
                        "RUNNING",
                        {
                            "job_type": "time-tags",
                            "job_id": claim["job"].get("job_id"),
                            "file_id": item.get("file_id"),
                        },
                        "time tag generation running",
                    )
                    self.stdout.write(f"time-tags job={claim['job'].get('job_id')} file={item.get('file_id')} started")
                    status = run_claimed_time_tag_item(claim)
                    update_worker_heartbeat(current_worker, queue_list, "IDLE", message=f"time-tags item {status}")
                    self.stdout.write(f"time-tags job={claim['job'].get('job_id')} file={item.get('file_id')} {status}")
                    did_work = True
            if not did_work:
                refresh_worker_job_statuses()
                update_worker_heartbeat(current_worker, queue_list, "IDLE", message="waiting for jobs")
                if once:
                    return
                time.sleep(sleep_seconds)
            elif once:
                return
