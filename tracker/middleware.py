import ipaddress

from django.http import HttpResponse

HEALTH_PATH = "/healthz"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HealthCheckMiddleware:
    """Answer platform health checks before Django's host validation.

    DigitalOcean App Platform probes the container by its internal IP (Host: 10.x.x.x:8080),
    which ALLOWED_HOSTS rightly rejects with a 400, so the deploy fails its health check.
    Real visitors always arrive by domain name, so a bare-IP Host (or /healthz) gets a plain
    "ok" and never reaches a page. Must be first in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.META.get("HTTP_HOST", "")
        host = raw[1:].split("]")[0] if raw.startswith("[") else raw.rsplit(":", 1)[0]
        if request.path == HEALTH_PATH or _is_ip(host):
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
