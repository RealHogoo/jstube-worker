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
        self.stdout.write(self.style.SUCCESS(f"media worker started id={current_worker} queues={','.join(sorted(queues))}"))
        while True:
            did_work = False
            if "youtube" in queues:
                claim = claim_youtube_item(current_worker, lease_seconds)
                if claim:
                    item = claim["item"]
                    self.stdout.write(f"youtube job={claim['job'].get('job_id')} video={item.get('youtube_video_id')} started")
                    status = run_claimed_youtube_item(claim)
                    self.stdout.write(f"youtube job={claim['job'].get('job_id')} video={item.get('youtube_video_id')} {status}")
                    did_work = True
            if "time-tags" in queues or "time_tags" in queues:
                claim = claim_time_tag_item(current_worker, lease_seconds)
                if claim:
                    item = claim["item"]
                    self.stdout.write(f"time-tags job={claim['job'].get('job_id')} file={item.get('file_id')} started")
                    status = run_claimed_time_tag_item(claim)
                    self.stdout.write(f"time-tags job={claim['job'].get('job_id')} file={item.get('file_id')} {status}")
                    did_work = True
            if not did_work:
                refresh_worker_job_statuses()
                if once:
                    return
                time.sleep(sleep_seconds)
            elif once:
                return
