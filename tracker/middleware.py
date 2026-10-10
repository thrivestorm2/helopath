import ipaddress

from django.http import HttpResponse

HEALTH_PATH = "/healthz"


def _is_probe_ip(host: str) -> bool:
    """A bare, non-loopback IP: how the platform's health checks address the container.
    Loopback is excluded so a developer browsing http://127.0.0.1:8000 gets real pages."""
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HealthCheckMiddleware:
    """Answer platform health checks before Django's host validation.

    DigitalOcean App Platform probes the container by its internal IP (Host: 10.x.x.x:8080),
    which ALLOWED_HOSTS rightly rejects with a 400, so the deploy fails its health check.
    Real visitors always arrive by domain name, so a bare non-loopback IP Host (or /healthz) gets
    a plain "ok" and never reaches a page. Must be first in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.META.get("HTTP_HOST", "")
        host = raw[1:].split("]")[0] if raw.startswith("[") else raw.rsplit(":", 1)[0]
        if request.path == HEALTH_PATH or _is_probe_ip(host):
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
