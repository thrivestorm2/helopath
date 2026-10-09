from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a user in the Viewer or Admin role (there is no self-signup)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--role", choices=["viewer", "admin"], default="viewer")
        parser.add_argument("--email", default="")

    def handle(self, *args, **opts):
        if User.objects.filter(username=opts["username"]).exists():
            raise CommandError("user exists")
        user = User(username=opts["username"], email=opts["email"], is_staff=opts["role"] == "admin")
        user.set_unusable_password()
        user.save()
        user.groups.add(Group.objects.get(name=opts["role"].capitalize()))
        self.stdout.write(self.style.SUCCESS(
            f"created {user.username} ({opts['role']}); set a password with: manage.py changepassword {user.username}"))
