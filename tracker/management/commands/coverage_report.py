from datetime import date, datetime, time, timedelta, timezone

from django.core.management.base import BaseCommand

from tracker.inference.legs import is_qualifying_visit
from tracker.models import CoverageComparison, IngestRun, Leg


class Command(BaseCommand):
    help = ("Milestone 8: compare resolved-landing rate and hospital-visiting aircraft per source on days ingested "
            "from both (e.g. ADS-B Exchange first-of-month samples vs adsb.lol). Legs are compared for the same "
            "candidate pool; results go to CoverageComparison, never into Aircraft or aggregates.")

    def add_arguments(self, parser):
        parser.add_argument("--sources", default="adsblol,adsbx")
        parser.add_argument("--day", action="append", type=date.fromisoformat)

    def handle(self, *args, **opts):
        sources = opts["sources"].split(",")
        days = opts["day"] or sorted(
            set.intersection(*[set(IngestRun.objects.filter(source=s, status="ok").values_list("day", flat=True))
                               for s in sources]))
        if not days:
            self.stderr.write("no day has a successful ingest for every source")
            return
        self.stdout.write(f"{'day':<12}{'source':<10}{'legs':>8}{'resolved':>10}{'rate':>8}{'visits':>8}{'aircraft':>10}")
        for day in days:
            start = datetime.combine(day, time.min, tzinfo=timezone.utc)
            for source in sources:
                legs = Leg.objects.filter(source=source, start_ts__gte=start, start_ts__lt=start + timedelta(days=1))
                visits = [leg for leg in legs.select_related("end_site") if is_qualifying_visit(leg)]
                row, _ = CoverageComparison.objects.update_or_create(day=day, source=source, defaults=dict(
                    legs=legs.count(), resolved_legs=legs.filter(end_site__isnull=False).count(),
                    qualifying_visits=len(visits), aircraft_with_visits=len({v.hex for v in visits})))
                rate = f"{row.resolved_rate:.0%}" if row.resolved_rate is not None else "-"
                self.stdout.write(f"{day!s:<12}{source:<10}{row.legs:>8}{row.resolved_legs:>10}{rate:>8}"
                                  f"{row.qualifying_visits:>8}{row.aircraft_with_visits:>10}")
