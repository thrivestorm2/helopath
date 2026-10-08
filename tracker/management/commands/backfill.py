from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.inference import pipeline
from tracker.ingest import day_done, ingest_day


class Command(BaseCommand):
    help = "Ingest the last N days, skipping days that already have a successful IngestRun (resumable)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.BACKFILL_DAYS)
        parser.add_argument("--source", default="adsblol")

    def handle(self, *args, **opts):
        today = timezone.now().astimezone(timezone.get_fixed_timezone(0)).date()
        failures = 0
        for back in range(opts["days"], 0, -1):
            day = today - timedelta(days=back)
            if day_done(day, opts["source"]):
                self.stdout.write(f"{day}: already done, skipping")
                continue
            try:
                # Inference runs once at the end; per-day it would rebuild missions 90 times.
                run = ingest_day(day, opts["source"], infer=False)
                self.stdout.write(f"{day}: kept {run.candidates_kept} in {run.duration_s:.0f} s")
            except Exception as e:
                failures += 1
                self.stderr.write(f"{day}: FAILED {e}")
        self.stdout.write("rebuilding derived tables from retained traces...")
        self.stdout.write(str(pipeline.rebuild(opts["source"])))
        if failures:
            self.stderr.write(f"{failures} day(s) failed; re-run to retry them")
