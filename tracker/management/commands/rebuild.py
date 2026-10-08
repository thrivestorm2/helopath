from django.core.management.base import BaseCommand

from tracker.inference import pipeline
from tracker.models import Leg


class Command(BaseCommand):
    help = "Drop and rebuild all derived tables for one source from retained raw traces."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="adsblol")

    def handle(self, *args, **opts):
        stats = pipeline.rebuild(opts["source"])
        by_state = {s: Leg.objects.filter(source=opts["source"], end_state=s).count() for s, _ in Leg.END_STATES}
        self.stdout.write(self.style.SUCCESS(f"rebuild {opts['source']}: {stats} legs by end state: {by_state}"))
