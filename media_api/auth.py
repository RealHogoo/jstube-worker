import hashlib
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.http import HttpRequest, JsonResponse


MEDIA_SERVICE = "MEDIA_SERVICE"
WEBHARD_SERVICE = "WEBHARD_SERVICE"
_user_cache: dict[str, tuple[float, "CurrentUser"]] = {}


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    roles: list[str]
    service_permissions: dict[str, list[str]]
    access_token: str = ""

    @property
    def is_admin(self) -> bool:
        return "ROLE_ADMIN" in self.roles or "ROLE_SUPER_ADMIN" in self.roles

    def has_permission(self, permission: str) -> bool:
        if self.is_admin:
            return True
        target = normalize_code(permission)
        return target in self.media_permissions()

    def has_any_media_permission(self) -> bool:
        if self.is_admin:
            return True
        return len(self.media_permissions()) > 0

    def media_permissions(self) -> list[str]:
        permissions: list[str] = []
        for service_code in (MEDIA_SERVICE, WEBHARD_SERVICE):
            permissions.extend(self.service_permissions.get(normalize_code(service_code)) or [])
        return permissions


def require_user(
    request: HttpRequest,
    permission: str | None = None,
    require_media_permission: bool = True,
) -> CurrentUser | JsonResponse:
    token, source = auth_token_with_source(request)
    if not token:
        return JsonResponse({"ok": False, "code": "UNAUTHORIZED", "message": "login is required"}, status=401)
    if source == "cookie" and is_cross_site_request(request):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "authentication cookie cannot be used for cross-site requests"}, status=403)

    current_user = fetch_current_user(token)
    if current_user is None:
        return JsonResponse({"ok": False, "code": "UNAUTHORIZED", "message": "login is invalid"}, status=401)
    service_status = fetch_service_status(token, MEDIA_SERVICE)
    if service_status and str(service_status.get("use_yn") or "").upper() == "N":
        return JsonResponse({"ok": False, "code": "SERVICE_DISABLED", "message": "미디어 서비스가 관리자에 의해 비활성화되었습니다."}, status=403)
    if require_media_permission and not current_user.has_any_media_permission():
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "media permission is required"}, status=403)
    if permission and not current_user.has_permission(permission):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "permission is required"}, status=403)
    return replace(current_user, access_token=token)


def auth_token(request: HttpRequest) -> str:
    token, _source = auth_token_with_source(request)
    return token


def auth_token_with_source(request: HttpRequest) -> tuple[str, str]:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :].strip(), "bearer"
    cookie = request.COOKIES.get("ACCESS_TOKEN", "").strip()
    return cookie, "cookie" if cookie else ""


def is_cross_site_request(request: HttpRequest) -> bool:
    sec_fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if sec_fetch_site == "cross-site":
        return True
    if sec_fetch_site in {"same-origin", "same-site", "none"}:
        return False
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if request.method.upper() in {"POST", "PATCH", "DELETE"} and not origin and not referer:
        return True
    return not is_same_origin(request, origin) or not is_same_origin(request, referer)


def is_same_origin(request: HttpRequest, source: str) -> bool:
    if not source or not source.strip():
        return True
    try:
        parsed = urlparse(source.strip())
    except ValueError:
        return False
    if not parsed.scheme or not parsed.hostname:
        return False
    request_scheme = "https" if request.is_secure() else "http"
    request_host, request_port = request_host_and_port(request, request_scheme)
    source_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return (
        parsed.scheme.lower() == request_scheme
        and parsed.hostname.lower() == request_host
        and request_port == int(source_port)
    )


def request_host_and_port(request: HttpRequest, scheme: str) -> tuple[str, int]:
    host_header = request.get_host()
    parsed = urlparse(f"//{host_header}")
    host = (parsed.hostname or host_header.split(":")[0]).lower()
    if parsed.port:
        return host, parsed.port
    return host, 443 if scheme == "https" else 80


def fetch_current_user(token: str) -> CurrentUser | None:
    cached = cached_current_user(token)
    if cached is not None:
        return cached
    url = f"{settings.MEDIA_CONFIG['ADMIN_SERVICE_BASE_URL']}/auth/me.json"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if body.get("ok") is not True or not isinstance(body.get("data"), dict):
        return None
    data: dict[str, Any] = body["data"]
    user_id = str(data.get("user_id") or "")
    if not user_id:
        return None
    current_user = CurrentUser(
        user_id=user_id,
        roles=[str(item) for item in data.get("roles") or []],
        service_permissions=normalize_permissions(data.get("service_permissions")),
        access_token=token,
    )
    cache_current_user(token, current_user)
    return replace(current_user, access_token=token)


def fetch_service_status(token: str, service_code: str) -> dict[str, Any] | None:
    url = f"{settings.MEDIA_CONFIG['ADMIN_SERVICE_BASE_URL']}/health/service/list.json"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
            timeout=3,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    items = body.get("data")
    if body.get("ok") is not True or not isinstance(items, list):
        return None
    normalized = normalize_code(service_code)
    for item in items:
        if isinstance(item, dict) and normalize_code(str(item.get("service_cd") or "")) == normalized:
            return item
    return None


def cached_current_user(token: str) -> CurrentUser | None:
    ttl = auth_cache_ttl()
    if ttl <= 0:
        return None
    cache_key = auth_cache_key(token)
    cached = _user_cache.get(cache_key)
    if not cached:
        return None
    expires_at, current_user = cached
    if expires_at <= monotonic():
        _user_cache.pop(cache_key, None)
        return None
    return replace(current_user, access_token=token)


def cache_current_user(token: str, current_user: CurrentUser) -> None:
    ttl = auth_cache_ttl()
    if ttl <= 0:
        return
    prune_user_cache()
    _user_cache[auth_cache_key(token)] = (monotonic() + ttl, replace(current_user, access_token=""))


def auth_cache_ttl() -> float:
    try:
        return max(float(settings.MEDIA_CONFIG.get("AUTH_CACHE_SECONDS") or 5), 0)
    except (TypeError, ValueError):
        return 5


def auth_cache_max_entries() -> int:
    try:
        return max(int(settings.MEDIA_CONFIG.get("AUTH_CACHE_MAX_ENTRIES") or 500), 1)
    except (TypeError, ValueError):
        return 500


def prune_user_cache() -> None:
    max_entries = auth_cache_max_entries()
    if len(_user_cache) < max_entries:
        return
    now = monotonic()
    expired = [key for key, (expires_at, _) in _user_cache.items() if expires_at <= now]
    for key in expired:
        _user_cache.pop(key, None)
    overflow = len(_user_cache) - max_entries + 1
    if overflow <= 0:
        return
    oldest_keys = sorted(_user_cache, key=lambda key: _user_cache[key][0])[:overflow]
    for key in oldest_keys:
        _user_cache.pop(key, None)


def auth_cache_key(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def normalize_permissions(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for service_code, permissions in raw.items():
        if not isinstance(permissions, list):
            continue
        result[normalize_code(str(service_code))] = [normalize_code(str(item)) for item in permissions]
    return result


def normalize_code(value: str) -> str:
    return value.strip().replace("-", "_").replace(" ", "_").upper()
