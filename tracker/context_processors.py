from .roles import is_admin


def roles(request):
    admin = is_admin(getattr(request, "user", None)) if hasattr(request, "user") else False
    return {"is_admin": admin, "rebuild_banner": _rebuild_banner() if admin else None}


def _rebuild_banner():
    from .rebuild_flag import banner
    return banner()
