from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.inference import pipeline
from tracker.ingest import day_done, ingest_day, process_rebuild_requests


class Command(BaseCommand):
    help = "Nightly job: ingest any of the last 3 UTC days not yet done, process queued rebuilds, purge."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="adsblol")

    def handle(self, *args, **opts):
        source = opts["source"]
        today = timezone.now().astimezone(timezone.get_fixed_timezone(0)).date()
        failed = False
        for back in (3, 2, 1):  # releases appear the following day; catch up on a missed night
            day = today - timedelta(days=back)
            if day_done(day, source):
                continue
            try:
                run = ingest_day(day, source)
                self.stdout.write(f"{day}: kept {run.candidates_kept} in {run.duration_s:.0f} s")
            except Exception as e:
                failed = True
                self.stderr.write(f"{day}: FAILED {e}")
        rebuilt = process_rebuild_requests()
        if rebuilt:
            self.stdout.write(f"rebuilt: {', '.join(sorted(rebuilt))}")
        removed = pipeline.purge_undiscovered(source, today)
        self.stdout.write(f"purged {removed} undiscovered trace files past retention")
        if failed:
            raise SystemExit(1)
