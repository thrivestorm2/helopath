from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand

from tracker.ingest import ingest_day
from tracker.sources import get_adapter


class Command(BaseCommand):
    help = "Scan one UTC day of a source and run inference on the kept aircraft."

    def add_arguments(self, parser):
        parser.add_argument("day", type=date.fromisoformat)
        parser.add_argument("--source", default="adsblol")
        parser.add_argument("--local-part", action="append", type=Path,
                            help="Read these local tar part files instead of downloading (repeat, in order)")
        parser.add_argument("--no-infer", action="store_true")

    def handle(self, *args, **opts):
        kwargs = {"local_parts": opts["local_part"]} if opts["local_part"] else {}
        adapter = get_adapter(opts["source"], **kwargs)
        run = ingest_day(opts["day"], opts["source"], adapter=adapter, infer=not opts["no_infer"])
        self.stdout.write(self.style.SUCCESS(
            f"{run.day} {run.source} {run.release_used}: scanned {run.members_scanned} members, kept "
            f"{run.candidates_kept} ({run.bytes_retained / 1e6:.1f} MB), streamed {run.bytes_streamed / 1e9:.2f} GB "
            f"in {run.duration_s:.0f} s"))
