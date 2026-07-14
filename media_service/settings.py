import os
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent.parent


def _is_local_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return True
    host = (parsed.hostname or "").lower()
    return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _is_local_host(value: str) -> bool:
    host = str(value or "").strip().split(":")[0].lower()
    return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _is_weak_secret(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) < 32 or text in {"dev-media-secret", "dev-media-internal-token"}


APP_ENV = os.environ.get("APP_ENV", os.environ.get("MEDIA_SERVICE_ENV", "local")).strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
DEBUG = os.environ.get("MEDIA_SERVICE_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get("MEDIA_SERVICE_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("MEDIA_SERVICE_SECRET_KEY is required in production")
    SECRET_KEY = "dev-media-secret"
if IS_PRODUCTION and not os.environ.get("WEBHARD_STORAGE_ROOT"):
    raise RuntimeError("WEBHARD_STORAGE_ROOT is required in production")
if IS_PRODUCTION and not os.environ.get("MEDIA_INTERNAL_API_TOKEN"):
    raise RuntimeError("MEDIA_INTERNAL_API_TOKEN is required in production")
ALLOWED_HOSTS = [item.strip() for item in os.environ.get("MEDIA_SERVICE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if item.strip()]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "media_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "media_api.middleware.SecurityHeaderMiddleware",
]

ROOT_URLCONF = "media_service.urls"
WSGI_APPLICATION = "media_service.wsgi.application"
ASGI_APPLICATION = "media_service.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "media-service.sqlite3",
    }
}

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_CONFIG = {
    "ADMIN_SERVICE_BASE_URL": os.environ.get("ADMIN_SERVICE_BASE_URL", "http://localhost:8081").rstrip("/"),
    "MEDIA_FRONTEND_BASE_URL": os.environ.get("MEDIA_FRONTEND_BASE_URL", "http://localhost:5174").rstrip("/"),
    "MEDIA_QR_BASE_URL": os.environ.get("MEDIA_QR_BASE_URL", os.environ.get("MEDIA_FRONTEND_BASE_URL", "http://localhost:5174")).rstrip("/"),
    "WEBHARD_PUBLIC_BASE_URL": os.environ.get("WEBHARD_PUBLIC_BASE_URL", "http://localhost:8083").rstrip("/"),
    "WEBHARD_INTERNAL_BASE_URL": os.environ.get("WEBHARD_INTERNAL_BASE_URL", os.environ.get("WEBHARD_PUBLIC_BASE_URL", "http://localhost:8083")).rstrip("/"),
    "MEDIA_INTERNAL_API_TOKEN": os.environ.get("MEDIA_INTERNAL_API_TOKEN", "" if IS_PRODUCTION else "dev-media-internal-token"),
    "WEBHARD_STORAGE_ROOT": os.environ.get("WEBHARD_STORAGE_ROOT", str(BASE_DIR.parent.parent / "webhard-service" / "storage")),
    "MEDIA_MONGO_URI": os.environ.get("MEDIA_MONGO_URI", "mongodb://localhost:27017"),
    "MEDIA_MONGO_DATABASE": os.environ.get("MEDIA_MONGO_DATABASE", "media_service"),
    "MEDIA_SYNC_LIMIT": int(os.environ.get("MEDIA_SYNC_LIMIT", "500")),
    "AUTH_CACHE_SECONDS": float(os.environ.get("MEDIA_AUTH_CACHE_SECONDS", "5")),
    "AUTH_CACHE_MAX_ENTRIES": int(os.environ.get("MEDIA_AUTH_CACHE_MAX_ENTRIES", "500")),
    "YOUTUBE_YTDLP_PATH": os.environ.get("YOUTUBE_YTDLP_PATH", ""),
    "YOUTUBE_FFMPEG_PATH": os.environ.get("YOUTUBE_FFMPEG_PATH", ""),
    "YOUTUBE_TOOL_DIR": os.environ.get("YOUTUBE_TOOL_DIR", str(BASE_DIR / ".runtime" / "tools")),
    "YOUTUBE_IMPORT_LIMIT": int(os.environ.get("YOUTUBE_IMPORT_LIMIT", "100")),
    "YOUTUBE_IMPORT_MAX_ITEMS": int(os.environ.get("YOUTUBE_IMPORT_MAX_ITEMS", "100")),
    "YOUTUBE_TIME_TAG_LIMIT": int(os.environ.get("YOUTUBE_TIME_TAG_LIMIT", "100")),
    "YOUTUBE_VIDEO_MAX_HEIGHT": int(os.environ.get("YOUTUBE_VIDEO_MAX_HEIGHT", "1080")),
    "YOUTUBE_VIDEO_CRF": int(os.environ.get("YOUTUBE_VIDEO_CRF", "23")),
    "YOUTUBE_VIDEO_PRESET": os.environ.get("YOUTUBE_VIDEO_PRESET", "veryfast"),
    "YOUTUBE_AUDIO_BITRATE": os.environ.get("YOUTUBE_AUDIO_BITRATE", "160k"),
}

CORS_ORIGINS = [item.strip() for item in os.environ.get("MEDIA_SERVICE_CORS_ORIGINS", "").split(",") if item.strip()]

if IS_PRODUCTION:
    for required_name in ("ADMIN_SERVICE_BASE_URL", "MEDIA_FRONTEND_BASE_URL", "MEDIA_QR_BASE_URL", "WEBHARD_PUBLIC_BASE_URL", "MEDIA_SERVICE_CORS_ORIGINS"):
        if not os.environ.get(required_name):
            raise RuntimeError(f"{required_name} is required in production")
    if DEBUG:
        raise RuntimeError("MEDIA_SERVICE_DEBUG must be false in production")
    if _is_weak_secret(SECRET_KEY):
        raise RuntimeError("MEDIA_SERVICE_SECRET_KEY must be at least 32 characters in production")
    if not ALLOWED_HOSTS or any(host == "*" or _is_local_host(host) for host in ALLOWED_HOSTS):
        raise RuntimeError("production allowed hosts must be explicit non-local hosts")
    if _is_weak_secret(MEDIA_CONFIG["MEDIA_INTERNAL_API_TOKEN"]):
        raise RuntimeError("MEDIA_INTERNAL_API_TOKEN must be a strong non-default token in production")
    if _is_local_url(MEDIA_CONFIG["MEDIA_FRONTEND_BASE_URL"]) or _is_local_url(MEDIA_CONFIG["MEDIA_QR_BASE_URL"]):
        raise RuntimeError("production media frontend and QR URLs must not point to localhost")
    if any(origin == "*" or _is_local_url(origin) for origin in CORS_ORIGINS):
        raise RuntimeError("production CORS origins must be explicit non-local origins")
