import hashlib
import json
import mimetypes
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import requests
from django.conf import settings
from .auth import CurrentUser
from .mongo import media_collection
from .webhard import check_webhard_internal_ready, register_youtube_file, sync_one_from_webhard

def preview_youtube(url: str) -> dict[str, Any]:
    info = load_youtube_info(url)
    items = extract_video_items(info)
    return {
        "source_type": info.get("_type") or "video",
        "title": info.get("title") or "",
        "playlist_id": info.get("id") if (info.get("_type") or "").lower() == "playlist" else "",
        "playlist_title": info.get("title") if (info.get("_type") or "").lower() == "playlist" else "",
        "items": items,
        "item_count": len(items),
    }

def import_youtube(url: str, current_user: CurrentUser, token: str, tags: list[str] | None = None) -> dict[str, Any]:
    if not token:
        raise RuntimeError("login token is required for webhard upload")
    info = load_youtube_info(url)
    items = extract_video_items(info)
    playlist_title = str(info.get("title") or "") if (info.get("_type") or "").lower() == "playlist" else ""
    playlist_id = str(info.get("id") or "") if (info.get("_type") or "").lower() == "playlist" else ""
    base_tags = normalize_import_tags(tags)
    downloaded = 0
    skipped = 0
    failed = 0
    results = []
    for item in items:
        video_id = str(item.get("youtube_video_id") or "").strip()
        if not video_id:
            skipped += 1
            continue
        try:
            result = import_youtube_item(item, current_user, base_tags, playlist_id, playlist_title)
            downloaded += 1
            results.append(result)
        except Exception as exc:
            failed += 1
            results.append({
                "youtube_video_id": video_id,
                "title": item.get("title") or video_id,
                "status": "FAILED",
                "message": str(exc)[:500],
            })
    if downloaded == 0 and failed > 0:
        first_failure = next((item for item in results if item.get("status") == "FAILED"), {})
        raise RuntimeError(str(first_failure.get("message") or "youtube download failed"))
    return {
        "source_type": "YOUTUBE_DOWNLOAD",
        "scanned_count": len(items),
        "downloaded_count": downloaded,
        "upserted_count": downloaded,
        "skipped_count": skipped,
        "failed_count": failed,
        "results": results,
    }

def import_youtube_item(
    item: dict[str, Any],
    current_user: CurrentUser,
    tags: list[str],
    playlist_id: str = "",
    playlist_title: str = "",
) -> dict[str, Any]:
    video_id = str(item.get("youtube_video_id") or "").strip()
    if not video_id:
        raise RuntimeError("youtube video id is required")
    downloaded_file = download_youtube_video(item)
    try:
        upload = save_to_webhard_storage(downloaded_file, item, current_user, tags)
        file_id = int(upload.get("file_id") or 0)
        if file_id <= 0:
            raise RuntimeError("webhard upload response does not include file_id")
        sync_one_from_webhard(current_user, file_id)
        apply_youtube_metadata(file_id, item, playlist_id, playlist_title, tags, str(upload.get("karaoke_number") or ""), str(upload.get("file_name") or ""))
        return {
            "youtube_video_id": video_id,
            "file_id": file_id,
            "title": item.get("title") or video_id,
            "status": "DOWNLOADED",
        }
    finally:
        cleanup_download_dir(video_id)

def check_download_tools() -> dict[str, Any]:
    yt_dlp = check_yt_dlp(auto_update=True)
    ffmpeg = check_ffmpeg(auto_install=True)
    webhard = check_webhard()
    required_ok = yt_dlp["installed"] and ffmpeg["installed"] and webhard["installed"]
    return {
        "ok_to_download": bool(required_ok),
        "tools": {
            "yt_dlp": yt_dlp,
            "ffmpeg": ffmpeg,
            "webhard": webhard,
        },
    }

def check_webhard() -> dict[str, Any]:
    base_url = str(settings.MEDIA_CONFIG.get("WEBHARD_INTERNAL_BASE_URL") or settings.MEDIA_CONFIG.get("WEBHARD_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base_url:
        return {
            "name": "webhard",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": "",
            "is_latest": None,
            "message": "webhard base url is not configured",
        }
    try:
        check_webhard_internal_ready()
        return {
            "name": "webhard",
            "installed": True,
            "path": base_url,
            "version": "ready",
            "latest_version": "",
            "is_latest": None,
            "message": "webhard internal api is ready",
        }
    except Exception as exc:
        return {
            "name": "webhard",
            "installed": False,
            "path": base_url,
            "version": "down",
            "latest_version": "",
            "is_latest": None,
            "message": f"webhard is not reachable: {exc}",
        }

def check_yt_dlp(auto_update: bool = False) -> dict[str, Any]:
    command = yt_dlp_command()
    if not command:
        return {
            "name": "yt-dlp",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": latest_yt_dlp_version(),
            "is_latest": False,
            "message": "yt-dlp is not installed",
        }
    version = command_output([command, "--version"])
    latest = latest_yt_dlp_version()
    is_latest = None if not latest else version_key(version) >= version_key(latest)
    update_message = ""
    if auto_update and latest and is_latest is False:
        previous_version = version
        updated = update_yt_dlp(command)
        update_message = updated.get("message") or ""
        command = yt_dlp_command() or command
        version = command_output([command, "--version"])
        is_latest = None if not latest else version_key(version) >= version_key(latest)
        if is_latest is not False and version != previous_version:
            update_message = f"updated yt-dlp from {previous_version} to {version}"
    return {
        "name": "yt-dlp",
        "installed": True,
        "path": command,
        "version": version,
        "latest_version": latest,
        "is_latest": is_latest,
        "message": yt_dlp_message(is_latest, latest, update_message),
    }

def check_ffmpeg(auto_install: bool = False) -> dict[str, Any]:
    command = ffmpeg_command()
    installed_by_service = False
    install_message = ""
    if not command and auto_install:
        installed = install_ffmpeg()
        command = installed.get("path") or ""
        installed_by_service = bool(command)
        install_message = installed.get("message") or ""
    if not command:
        return {
            "name": "ffmpeg",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": latest_ffmpeg_release(),
            "is_latest": False,
            "installed_by_service": False,
            "message": install_message or "ffmpeg is not installed",
        }
    first_line = command_output([command, "-version"]).splitlines()[0:1]
    version = first_line[0] if first_line else ""
    latest = latest_ffmpeg_release()
    return {
        "name": "ffmpeg",
        "installed": True,
        "path": command,
        "version": version,
        "latest_version": latest,
        "is_latest": None,
        "installed_by_service": installed_by_service,
        "message": install_message or ("installed; compare release manually" if latest else "installed"),
    }

def load_youtube_info(url: str) -> dict[str, Any]:
    ensure_allowed_youtube_url(url)
    command = yt_dlp_command()
    if not command:
        raise RuntimeError("yt-dlp is not installed")
    limit = int(settings.MEDIA_CONFIG.get("YOUTUBE_IMPORT_LIMIT") or 200)
    result = subprocess.run(
        [
            command,
            *yt_dlp_js_runtime_args(),
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--no-warnings",
            "--ignore-errors",
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError((result.stderr or result.stdout or "youtube analysis failed").strip()[:500])
    import json
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("youtube analysis response is invalid")
    return data

def load_youtube_timeline_info(url: str) -> dict[str, Any]:
    ensure_allowed_youtube_url(url)
    command = yt_dlp_command()
    if not command:
        raise RuntimeError("yt-dlp is not installed")
    result = subprocess.run(
        [
            command,
            *yt_dlp_js_runtime_args(),
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "--ignore-errors",
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError((result.stderr or result.stdout or "youtube timeline analysis failed").strip()[:500])
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("youtube timeline response is invalid")
    return data

def youtube_time_tags(url: str, fallback_description: str = "") -> list[str]:
    info = load_youtube_timeline_info(url)
    tags: list[str] = []
    for chapter in info.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        seconds = chapter.get("start_time")
        if isinstance(seconds, (int, float)):
            label = str(chapter.get("title") or "타임라인").strip()
            tags.append(f"{format_time_tag_seconds(float(seconds))} {label}")
    description = str(info.get("description") or fallback_description or "")
    tags.extend(time_tags_from_text(description))
    return unique_time_tags(tags)[:30]

def video_frame_time_tags(storage_path: str) -> list[str]:
    lyric_start = detect_first_lyric_start(storage_path)
    if lyric_start is None:
        return []
    return [f"{format_time_tag_seconds(float(lyric_start))} 가사시작"]

def detect_first_lyric_start(storage_path: str) -> int | None:
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    source = Path(str(storage_path or "")).resolve()
    root = webhard_storage_root()
    assert_path_under_root(source, root)
    if not source.exists():
        raise RuntimeError("video file was not found")
    tool_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve()
    probe_root = tool_dir / "frame-probes"
    probe_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{source.stem[:12]}-", dir=probe_root) as temp_dir:
        output_pattern = str(Path(temp_dir) / "f%04d.jpg")
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-t",
                "90",
                "-vf",
                "fps=1,scale=240:-1",
                output_pattern,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "video frame analysis failed").strip()[-500:])
        frames = sorted(Path(temp_dir).glob("f*.jpg"))
        return first_lyric_second_from_frames(frames)

def first_lyric_second_from_frames(frames: list[Path]) -> int | None:
    if len(frames) < 8:
        return None
    scores = [lyric_frame_score(path) for path in frames]
    baseline = sorted(scores[:8])[len(scores[:8]) // 2]
    threshold = max(baseline + 0.25, baseline * 1.55)
    for index in range(8, len(scores) - 2):
        window = (scores[index] + scores[index + 1] + scores[index + 2]) / 3
        if window >= threshold:
            return max(index, 0)
    return None

def lyric_frame_score(path: Path) -> float:
    try:
        from PIL import Image, ImageFilter
    except ImportError as exc:
        raise RuntimeError("Pillow is not installed") from exc
    with Image.open(path).convert("RGB") as image:
        width, height = image.size
        zones = [
            (0, int(height * 0.25), width, int(height * 0.58)),
            (0, int(height * 0.58), width, height),
        ]
        bright = 0
        cyan = 0
        edge = 0
        pixel_count = 0
        for box in zones:
            crop = image.crop(box)
            gray = crop.convert("L")
            edges = gray.filter(ImageFilter.FIND_EDGES)
            pixels = list(crop.getdata())
            edge_pixels = list(edges.getdata())
            pixel_count += len(pixels)
            bright += sum(1 for red, green, blue in pixels if red > 180 and green > 180 and blue > 180)
            cyan += sum(1 for red, green, blue in pixels if green > 130 and blue > 130 and blue > red + 20)
            edge += sum(1 for value in edge_pixels if value > 70)
        if pixel_count <= 0:
            return 0
        return ((bright * 1.2) + cyan + (edge * 2.0)) / pixel_count

def download_youtube_video(item: dict[str, Any]) -> Path:
    command = yt_dlp_command()
    if not command:
        raise RuntimeError("yt-dlp is not installed")
    video_id = str(item.get("youtube_video_id") or "").strip()
    video_url = str(item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
    ensure_allowed_youtube_url(video_url)
    target_dir = youtube_download_dir(video_id)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    command_line = [
        command,
        *yt_dlp_js_runtime_args(),
        "--no-playlist",
        "--no-warnings",
        "-f",
        youtube_format_selector(),
        "--merge-output-format",
        "mp4",
        "-o",
        str(target_dir / "%(id)s.%(ext)s"),
    ]
    ffmpeg = ffmpeg_command()
    if ffmpeg:
        command_line.extend(["--ffmpeg-location", str(Path(ffmpeg).parent)])
    command_line.append(video_url)
    result = subprocess.run(
        command_line,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60 * 60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "youtube download failed").strip()[-1000:])
    candidates = sorted([path for path in target_dir.glob(f"{safe_glob(video_id)}.*") if path.is_file()], key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        candidates = sorted([path for path in target_dir.iterdir() if path.is_file()], key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        raise RuntimeError("downloaded file was not found")
    return normalize_youtube_mp4(candidates[0])


def ensure_browser_playable_mp4(path: Path) -> Path:
    info = media_probe(path)
    if is_browser_playable_mp4(info):
        return path
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to convert youtube video for browser playback")
    target = path.with_name(f"{path.stem}.browser.mp4")
    video_codec = first_codec(info, "video")
    command_line = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "copy" if video_codec == "h264" else "libx264",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command_line, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60 * 60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "browser playback conversion failed").strip()[-1000:])
    return target


def normalize_youtube_mp4(path: Path) -> Path:
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to normalize youtube video")
    target = path.with_name(f"{path.stem}.normalized.mp4")
    max_height = max(int(settings.MEDIA_CONFIG.get("YOUTUBE_VIDEO_MAX_HEIGHT") or 720), 240)
    crf = min(max(int(settings.MEDIA_CONFIG.get("YOUTUBE_VIDEO_CRF") or 23), 18), 30)
    preset = str(settings.MEDIA_CONFIG.get("YOUTUBE_VIDEO_PRESET") or "veryfast")
    audio_bitrate = str(settings.MEDIA_CONFIG.get("YOUTUBE_AUDIO_BITRATE") or "160k")
    scale_filter = f"scale=-2:'min({max_height},ih)':force_original_aspect_ratio=decrease,format=yuv420p"
    command_line = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        scale_filter,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-crf",
        str(crf),
        "-tag:v",
        "avc1",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command_line, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60 * 60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "youtube normalization failed").strip()[-1000:])
    if not is_browser_playable_mp4(media_probe(target)):
        raise RuntimeError("normalized youtube video is not browser playable")
    return target


def youtube_format_selector() -> str:
    max_height = max(int(settings.MEDIA_CONFIG.get("YOUTUBE_VIDEO_MAX_HEIGHT") or 720), 240)
    height_filter = f"[height<={max_height}]"
    return (
        f"bv*{height_filter}[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*{height_filter}[vcodec^=avc1]+ba[acodec^=mp4a]/"
        "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"b{height_filter}[ext=mp4]/"
        "b[ext=mp4]/b"
    )


def media_probe(path: Path) -> dict[str, Any]:
    ffprobe = ffprobe_command()
    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect youtube video")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,profile,pix_fmt,width,height,level",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "media probe failed").strip()[-500:])
    try:
        return json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError("media probe response is invalid") from exc


def is_browser_playable_mp4(info: dict[str, Any]) -> bool:
    format_name = str((info.get("format") or {}).get("format_name") or "")
    if "mp4" not in format_name and "mov" not in format_name:
        return False
    video_codec = first_codec(info, "video")
    audio_codec = first_codec(info, "audio")
    pix_fmt = first_stream_value(info, "video", "pix_fmt")
    return video_codec == "h264" and pix_fmt in {"", "yuv420p"} and audio_codec in {"", "aac", "mp3"}


def first_codec(info: dict[str, Any], codec_type: str) -> str:
    return first_stream_value(info, codec_type, "codec_name").lower()


def first_stream_value(info: dict[str, Any], codec_type: str, key: str) -> str:
    for stream in info.get("streams") or []:
        if str(stream.get("codec_type") or "") == codec_type:
            return str(stream.get(key) or "")
    return ""

def upload_to_webhard(path: Path, item: dict[str, Any], token: str) -> dict[str, Any]:
    base_url = str(settings.MEDIA_CONFIG.get("WEBHARD_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("webhard base url is not configured")
    mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    upload_name = safe_file_name(str(item.get("title") or path.stem), str(item.get("youtube_video_id") or path.stem), path.suffix or ".mp4")
    with open(path, "rb") as handle:
        response = requests.post(
            f"{base_url}/file/upload.json",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (upload_name, handle, mime_type)},
            data={"original_created_at": datetime.now(timezone.utc).isoformat()},
            timeout=60 * 30,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"webhard upload response is invalid: {response.status_code}") from exc
    if not response.ok or body.get("ok") is not True:
        raise RuntimeError(str(body.get("message") or f"webhard upload failed: HTTP {response.status_code}"))
    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("webhard upload data is invalid")
    return data

def save_to_webhard_storage(path: Path, item: dict[str, Any], current_user: CurrentUser, tags: list[str]) -> dict[str, Any]:
    storage_root = webhard_storage_root()
    owner_dir = safe_path_segment(current_user.user_id)
    now = datetime.now(timezone.utc)
    relative_dir = Path(owner_dir) / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
    target_dir = storage_root / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix if path.suffix else ".mp4"
    stored_name = f"{uuid.uuid4()}{suffix.lower()}"
    storage_path = target_dir / stored_name
    assert_path_under_root(storage_path, storage_root)
    thumbnail_path = None
    try:
        shutil.copy2(path, storage_path)
        file_name = safe_file_name(str(item.get("title") or path.stem), str(item.get("youtube_video_id") or path.stem), suffix)
        file_size = storage_path.stat().st_size
        content_type = mimetypes.guess_type(file_name)[0] or "video/mp4"
        content_sha256 = sha256_file(storage_path)
        thumbnail_path = create_direct_video_thumbnail(storage_path, current_user.user_id, now)
        file_id = insert_webhard_file(
            current_user=current_user,
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            storage_path=storage_path,
            public_path=f"/storage/{relative_dir.as_posix()}/{stored_name}",
            thumbnail_path=thumbnail_path,
            original_created_at=now,
            content_sha256=content_sha256,
            is_karaoke="노래방" in tags,
        )
        return {
            "file_id": file_id,
            "public_path": f"/storage/{relative_dir.as_posix()}/{stored_name}",
            "original_created_at": now.isoformat(),
            "content_kind": "VIDEO",
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
            "content_sha256": content_sha256,
            "duplicate_count": 0,
            "duplicate_files": [],
        }
    except Exception:
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)
        if thumbnail_path and Path(thumbnail_path).exists():
            Path(thumbnail_path).unlink(missing_ok=True)
        raise

def insert_webhard_file(
    current_user: CurrentUser,
    file_name: str,
    file_size: int,
    content_type: str,
    storage_path: Path,
    public_path: str,
    thumbnail_path: Path | None,
    original_created_at: datetime,
    content_sha256: str,
    is_karaoke: bool,
) -> int:
    data = register_youtube_file(
        current_user,
        {
            "file_name": file_name,
            "file_size": file_size,
            "content_type": content_type,
            "storage_path": str(storage_path),
            "public_path": public_path,
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
            "media_public_yn": current_user.is_admin,
            "original_created_at": original_created_at.isoformat(),
            "content_sha256": content_sha256,
            "is_karaoke": is_karaoke,
        },
    )
    return int(data.get("file_id") or 0)

def create_direct_video_thumbnail(storage_path: Path, owner_user_id: str, created_at: datetime) -> Path | None:
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        return None
    owner_dir = safe_path_segment(owner_user_id)
    storage_root = webhard_storage_root()
    thumbnail_dir = storage_root / owner_dir / ".thumbs" / str(created_at.year) / f"{created_at.month:02d}" / f"{created_at.day:02d}"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    target = thumbnail_dir / f"{storage_path.stem}.webp"
    assert_path_under_root(target, storage_root)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(storage_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=420:315:force_original_aspect_ratio=increase,crop=420:315",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return target if result.returncode == 0 and target.exists() else None

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def safe_path_segment(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "").strip())
    return normalized or "unknown"


def ensure_allowed_youtube_url(value: str) -> None:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError as exc:
        raise RuntimeError("youtube url is invalid") from exc
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
    if parsed.scheme != "https" or host not in allowed_hosts:
        raise RuntimeError("only youtube urls are allowed")

def webhard_storage_root() -> Path:
    configured = str(settings.MEDIA_CONFIG.get("WEBHARD_STORAGE_ROOT") or "").strip()
    if not configured:
        raise RuntimeError("webhard storage root is not configured")
    return Path(configured).resolve()

def assert_path_under_root(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise RuntimeError("resolved file path is outside webhard storage root")

def apply_youtube_metadata(file_id: int, item: dict[str, Any], playlist_id: str, playlist_title: str, tags: list[str], karaoke_number: str = "", file_name: str = "") -> None:
    video_id = str(item.get("youtube_video_id") or "")
    title = str(item.get("title") or video_id)
    media_tags = youtube_media_tags(" ".join([title, file_name, karaoke_number]), tags)
    media_collection().update_one(
        {"webhard_file_id": file_id},
        {
            "$set": {
                "source_type": "YOUTUBE_DOWNLOAD",
                "owner_is_admin": True,
                "youtube_video_id": video_id,
                "youtube_url": item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                "youtube_playlist_id": playlist_id,
                "youtube_playlist_title": playlist_title,
                "title": title,
                "display_name": title,
                "description": item.get("description") or "",
                "channel_name": item.get("channel_name") or "YouTube",
                "album": playlist_title,
                "tags": media_tags,
                "synced_at": datetime.now(timezone.utc),
            }
        },
    )

def normalize_import_tags(tags: list[str] | None) -> list[str]:
    result = ["youtube"]
    for tag in tags or []:
        normalized = str(tag or "").strip()
        if normalized and normalized not in result:
            result.append(normalized[:40])
    return result[:30]

def youtube_media_tags(title: str, tags: list[str]) -> list[str]:
    result = list(tags)
    if "노래방" not in result:
        return result
    match = re.search(r"\bKY[.\-_ ]?(\d{4,7})\b", title, re.IGNORECASE)
    ky_tag = f"KY.{match.group(1)}" if match else "0000"
    if ky_tag not in result:
        result.append(ky_tag)
    return result[:30]

def time_tags_from_text(value: str) -> list[str]:
    result = []
    for line in str(value or "").splitlines():
        text = line.strip()
        match = re.search(r"(?<!\d)(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?!\d)", text)
        if not match:
            continue
        seconds = time_match_seconds(match)
        label = (text[:match.start()] + " " + text[match.end():]).strip(" -–—:|")
        result.append(f"{format_time_tag_seconds(seconds)} {label or '타임라인'}")
    return result

def time_match_seconds(match: re.Match) -> float:
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return float((hours * 3600) + (minutes * 60) + seconds)

def format_time_tag_seconds(value: float) -> str:
    total = max(int(round(value)), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def unique_time_tags(tags: list[str]) -> list[str]:
    result = []
    seen_seconds = set()
    for tag in tags:
        text = str(tag or "").strip()[:40]
        marker = re.search(r"(?<!\d)(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?!\d)", text)
        if not marker:
            continue
        key = int(time_match_seconds(marker))
        if key in seen_seconds:
            continue
        seen_seconds.add(key)
        result.append(text)
    result.sort(key=lambda item: time_match_seconds(re.search(r"(?<!\d)(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?!\d)", item)))
    return result

def youtube_download_dir(video_id: str) -> Path:
    base_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve().parent / "youtube-downloads"
    digest = hashlib.sha1(video_id.encode("utf-8")).hexdigest()[:12]
    safe_id = safe_path_segment(video_id)[:48]
    return base_dir / f"{safe_id}-{digest}"

def cleanup_download_dir(video_id: str) -> None:
    target_dir = youtube_download_dir(video_id)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)

def safe_file_name(title: str, video_id: str, suffix: str) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" .")[:160]
    if not base:
        base = video_id or "youtube-video"
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{base}{ext}"

def safe_glob(value: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", value)

def yt_dlp_command() -> str | None:
    configured = str(settings.MEDIA_CONFIG.get("YOUTUBE_YTDLP_PATH") or "").strip()
    if configured:
        return existing_command(configured)
    return shutil.which("yt-dlp")

def yt_dlp_js_runtime_args() -> list[str]:
    deno = shutil.which("deno")
    if deno:
        return ["--js-runtimes", f"deno:{deno}", "--remote-components", "ejs:github"]
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        return ["--js-runtimes", f"node:{node}", "--remote-components", "ejs:github"]
    return []

def ffmpeg_command() -> str | None:
    configured = str(settings.MEDIA_CONFIG.get("YOUTUBE_FFMPEG_PATH") or "").strip()
    if configured:
        return existing_command(configured)
    bundled = bundled_ffmpeg_command()
    if bundled:
        return bundled
    return shutil.which("ffmpeg")

def ffprobe_command() -> str | None:
    configured = str(settings.MEDIA_CONFIG.get("YOUTUBE_FFMPEG_PATH") or "").strip()
    if configured:
        candidate = Path(configured).with_name("ffprobe")
        if candidate.exists():
            return str(candidate)
    ffmpeg = ffmpeg_command()
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe")
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffprobe")

def existing_command(value: str) -> str | None:
    path = Path(value)
    if path.is_absolute() or "\\" in value or "/" in value:
        return str(path) if path.exists() else None
    return shutil.which(value)

def bundled_ffmpeg_command() -> str | None:
    tool_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve()
    candidates = list(tool_dir.glob("ffmpeg/**/bin/ffmpeg.exe")) + list(tool_dir.glob("ffmpeg/**/ffmpeg.exe"))
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return str(existing[0])

def install_ffmpeg() -> dict[str, str]:
    if platform.system().lower() != "windows":
        return {"path": "", "message": "automatic ffmpeg install is only supported on Windows in this environment"}
    try:
        asset = latest_ffmpeg_windows_asset()
        if not asset:
            return {"path": "", "message": "ffmpeg download asset was not found"}
        tool_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve()
        install_dir = tool_dir / "ffmpeg"
        install_dir.mkdir(parents=True, exist_ok=True)
        archive_path = install_dir / "ffmpeg-latest.zip"
        download_file(asset["browser_download_url"], archive_path)
        verify_download_digest(archive_path, str(asset.get("digest") or ""))
        extract_dir = install_dir / "latest"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "r") as archive:
            safe_extract_zip(archive, extract_dir)
        command = bundled_ffmpeg_command()
        if not command:
            return {"path": "", "message": "ffmpeg archive was extracted but ffmpeg.exe was not found"}
        return {"path": command, "message": "ffmpeg was installed automatically"}
    except Exception as exc:
        return {"path": "", "message": f"ffmpeg auto install failed: {exc}"}

def update_yt_dlp(command: str) -> dict[str, str]:
    attempts = [
        [command, "-U"],
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
    ]
    messages = []
    for command_line in attempts:
        try:
            result = subprocess.run(
                command_line,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except Exception as exc:
            messages.append(str(exc))
            continue
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            return {"message": first_line(output) or "yt-dlp update completed"}
        if output:
            messages.append(first_line(output))
    return {"message": "yt-dlp update failed: " + "; ".join([message for message in messages if message][:2])}


def yt_dlp_message(is_latest: bool | None, latest: str, update_message: str = "") -> str:
    if is_latest is False:
        base = f"latest yt-dlp is {latest}"
        return f"{base}; {update_message}" if update_message else base
    return update_message or "ok"


def first_line(value: str) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return lines[0] if lines else ""


def latest_ffmpeg_windows_asset() -> dict[str, Any] | None:
    response = requests.get("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest", timeout=12)
    response.raise_for_status()
    assets = response.json().get("assets") or []
    candidates = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip") and "win64" in name and "gpl" in name and "shared" not in name:
            candidates.append(asset)
    if not candidates:
        return None
    candidates.sort(key=lambda asset: str(asset.get("name") or ""))
    return candidates[0]

def download_file(url: str, target: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(target, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

def verify_download_digest(path: Path, expected_digest: str) -> None:
    prefix = "sha256:"
    if not expected_digest.startswith(prefix):
        raise RuntimeError("ffmpeg download digest is missing")
    expected_sha256 = expected_digest[len(prefix):].lower()
    actual_sha256 = sha256_file(path).lower()
    if actual_sha256 != expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg download digest verification failed")

def safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (root / member.filename).resolve()
        if member_path != root and root not in member_path.parents:
            raise RuntimeError("ffmpeg archive contains an unsafe path")
    archive.extractall(root)

def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return ""
    return (result.stdout or result.stderr or "").strip()

def latest_yt_dlp_version() -> str:
    try:
        response = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=8)
        if response.ok:
            return str((response.json().get("info") or {}).get("version") or "")
    except Exception:
        return ""
    return ""

def latest_ffmpeg_release() -> str:
    try:
        response = requests.get("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest", timeout=8)
        if response.ok:
            return str(response.json().get("tag_name") or response.json().get("name") or "")
    except Exception:
        return ""
    return ""

def version_key(value: str) -> tuple[int, ...]:
    parts = []
    for part in str(value or "").replace("-", ".").split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)

def extract_video_items(info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = info.get("entries")
    if isinstance(entries, list):
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                item = video_item(entry)
                if item.get("youtube_video_id"):
                    result.append(item)
        return result
    item = video_item(info)
    return [item] if item.get("youtube_video_id") else []

def video_item(raw: dict[str, Any]) -> dict[str, Any]:
    video_id = str(raw.get("id") or raw.get("url") or "").strip()
    if "youtube.com/watch" in video_id or "youtu.be/" in video_id:
        video_id = video_id.rstrip("/").split("/")[-1].split("v=")[-1].split("&")[0]
    thumbnail_url = raw.get("thumbnail") or best_thumbnail(raw.get("thumbnails"))
    webpage_url = raw.get("webpage_url") or raw.get("url") or ""
    if video_id and not str(webpage_url).startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "youtube_video_id": video_id,
        "title": raw.get("title") or video_id,
        "description": raw.get("description") or "",
        "duration": raw.get("duration"),
        "channel_name": raw.get("channel") or raw.get("uploader") or "",
        "thumbnail_url": thumbnail_url,
        "webpage_url": webpage_url,
    }

def best_thumbnail(thumbnails: Any) -> str:
    if not isinstance(thumbnails, list) or not thumbnails:
        return ""
    candidates = [item for item in thumbnails if isinstance(item, dict) and item.get("url")]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0), reverse=True)
    return str(candidates[0].get("url") or "")
