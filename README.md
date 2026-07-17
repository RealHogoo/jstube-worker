# jstube-worker

Background worker for the JsTube media service.

The worker uses the same Django codebase shape as `jsTube-api`, but it runs the
`media_worker` management command instead of serving HTTP. Source code only
belongs in this repository. Do not commit production `.env` files, tokens,
database dumps, media files, or private keys.

## Role

- Claim MongoDB-backed YouTube import jobs.
- Download/import selected YouTube videos.
- Normalize downloaded videos with ffmpeg before registering them in webhard.
- Register uploaded media through the webhard internal API.
- Generate karaoke time tags.
- Publish worker heartbeat/status documents for the admin UI.

## Queue Behavior

The worker only claims items in `QUEUED` state.

`FAILED` items remain failed until a user explicitly starts or recreates work.
This prevents a broken item from being retried forever and burning CPU.

Long-running `RUNNING` items whose lease expires are marked `FAILED` with a
stale-worker message. They are not automatically claimed again unless they are
changed back to `QUEUED`.

## Replicas

The current low-resource production default is one worker replica:

```sh
JSTUBE_WORKER_REPLICAS=1
```

The API can run more replicas, but ffmpeg/download work should stay on the
worker and is normally scheduled conservatively.

## Runtime

Required runtime values are provided by `jsDeploy`:

- `MEDIA_SERVICE_SECRET_KEY`
- `MEDIA_MONGO_URI`
- `MEDIA_MONGO_DATABASE`
- `MEDIA_INTERNAL_API_TOKEN`
- `WEBHARD_INTERNAL_BASE_URL`
- `WEBHARD_STORAGE_ROOT`
- `YOUTUBE_YTDLP_PATH`
- `YOUTUBE_FFMPEG_PATH`

Useful worker controls:

- `MEDIA_WORKER_QUEUES=youtube,time-tags`
- `MEDIA_WORKER_POLL_SECONDS=2`
- `MEDIA_WORKER_LEASE_SECONDS=7200`
- `MEDIA_WORKER_HEARTBEAT_SECONDS=15`
- `MEDIA_WORKER_STALE_SECONDS=60`
- `MEDIA_WORKER_ID=<optional-stable-id>`

## Local

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py check
python manage.py media_worker
```
