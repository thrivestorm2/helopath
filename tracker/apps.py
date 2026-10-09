from django.apps import AppConfig
from django.db.models.signals import post_migrate


class TrackerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tracker"

    def ready(self):
        from .roles import ensure_groups

        post_migrate.connect(ensure_groups, sender=self)
