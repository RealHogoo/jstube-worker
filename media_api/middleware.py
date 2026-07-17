from django.conf import settings
from django.utils.cache import patch_vary_headers


class SecurityHeaderMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "DENY"
        response["X-Permitted-Cross-Domain-Policies"] = "none"
        response["Referrer-Policy"] = "no-referrer"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["Cross-Origin-Opener-Policy"] = "same-origin"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if request.is_secure() or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
            response["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        origin = request.headers.get("Origin", "")
        if origin and origin in settings.CORS_ORIGINS:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            patch_vary_headers(response, ("Origin",))
        return response
