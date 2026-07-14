# jstube-worker

Distributed background worker for the JsTube media service.

The worker consumes MongoDB-backed media jobs created by `jsTube-api` and processes:

- YouTube download/import jobs
- MP4 browser-normalization through ffmpeg
- Webhard registration/upload handoff
- Karaoke time-tag generation

The worker does not expose an HTTP port. Run multiple replicas with Docker Compose scaling, for example:

```sh
docker compose up --scale jstube-worker=3 -d jstube-worker
```

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
- `MEDIA_WORKER_ID=<optional-stable-id>`

Do not commit production `.env` files, tokens, database dumps, media files, or private keys.
