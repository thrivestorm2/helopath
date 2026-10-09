from django.core.management.base import BaseCommand

from tracker.seed import seed_sites


class Command(BaseCommand):
    help = "Seed Site rows from FAA NASR APT_BASE.csv, filtered to the bounding box (all marked reviewed=false)."

    def add_arguments(self, parser):
        parser.add_argument("apt_base_csv")

    def handle(self, *args, **opts):
        with open(opts["apt_base_csv"], "rb") as fh:
            c, u, s = seed_sites(fh)
        self.stdout.write(self.style.SUCCESS(f"created {c}, updated {u}, skipped {s} (reviewed or non-seed)"))
