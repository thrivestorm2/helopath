from .roles import is_admin


def roles(request):
    return {"is_admin": is_admin(getattr(request, "user", None)) if hasattr(request, "user") else False}
