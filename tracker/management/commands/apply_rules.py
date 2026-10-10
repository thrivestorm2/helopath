from django.core.management.base import BaseCommand

from tracker.ops import queue_refresh
from tracker.rules import apply_auto_confirm_rules


class Command(BaseCommand):
    help = "Apply auto-confirm rules to discovered aircraft; queue a refresh if any changed. Safe on every start."

    def handle(self, *args, **opts):
        n = apply_auto_confirm_rules()
        if n:
            queue_refresh()
        self.stdout.write(f"auto-confirmed {n} aircraft by rule")
