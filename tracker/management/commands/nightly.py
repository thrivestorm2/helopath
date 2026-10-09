from django.core.management.base import BaseCommand

from tracker.ingest import run_nightly


class Command(BaseCommand):
    help = "Nightly job: ingest any of the last 3 UTC days not yet done, process queued rebuilds, purge."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="adsblol")

    def handle(self, *args, **opts):
        if not run_nightly(opts["source"], log=self.stdout.write):
            raise SystemExit(1)
