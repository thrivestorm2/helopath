"""Two roles, as Django groups: Viewer (read-only pages) and Admin (review + edits)."""
from functools import wraps

from django.core.exceptions import PermissionDenied

VIEWER, ADMIN = "Viewer", "Admin"
ADMIN_EDITABLE = ["site", "operator", "aircraft", "landingcluster"]


def ensure_groups(**kwargs):
    from django.contrib.auth.models import Group, Permission

    Group.objects.get_or_create(name=VIEWER)
    admin, _ = Group.objects.get_or_create(name=ADMIN)
    perms = Permission.objects.filter(content_type__app_label="tracker", content_type__model__in=ADMIN_EDITABLE)
    # Deactivate instead of delete: admins get add/change/view but not delete.
    admin.permissions.set([p for p in perms if not p.codename.startswith("delete_")])
    admin.permissions.add(*Permission.objects.filter(content_type__app_label="admin", codename="view_logentry"))


def is_admin(user):
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name=ADMIN).exists())


def admin_required(view):
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return inner
