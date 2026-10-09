import os

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Create the first admin login from HELOPATH_ADMIN_USERNAME (default 'admin') and "
            "HELOPATH_ADMIN_PASSWORD, only if no superuser exists yet. Safe to run on every deploy.")

    def handle(self, *args, **opts):
        if User.objects.filter(is_superuser=True).exists():
            self.stdout.write("admin already exists; nothing to do")
            return
        password = os.environ.get("HELOPATH_ADMIN_PASSWORD", "")
        if not password:
            self.stderr.write("HELOPATH_ADMIN_PASSWORD is not set; no admin created")
            return
        user = User.objects.create_superuser(os.environ.get("HELOPATH_ADMIN_USERNAME", "admin"), password=password)
        user.groups.add(Group.objects.get_or_create(name="Admin")[0])
        self.stdout.write(f"created admin login '{user.username}'")
