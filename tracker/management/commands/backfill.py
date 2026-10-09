from django.conf import settings
from django.core.management.base import BaseCommand

from tracker.ingest import run_backfill


class Command(BaseCommand):
    help = "Ingest the last N days, skipping days that already have a successful IngestRun (resumable)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.BACKFILL_DAYS)
        parser.add_argument("--source", default="adsblol")

    def handle(self, *args, **opts):
        run_backfill(opts["days"], opts["source"], log=self.stdout.write)
