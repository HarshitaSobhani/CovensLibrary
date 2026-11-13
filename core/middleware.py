"""Response headers that keep the browser from doing anything the application does not need.

The application is local (127.0.0.1) and its pages load only their own scripts, styles, images and
fonts, so the Content-Security-Policy allows nothing else: no inline scripts, no remote scripts,
no framing, no plug-ins. Inline *styles* are allowed because the templates use them."""

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            # personal data: browsers and proxies must not keep copies of authenticated pages
            response.setdefault("Cache-Control", "no-store")
        return response
