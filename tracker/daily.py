"""The dashboard's daily summary: notable figures for the latest full local day whose data has arrived.

ADS-B history arrives one UTC day at a time, and a local (Central) day spans two UTC days, so the day summarised is
the latest local date D whose UTC days D and D+1 have both been ingested. Each headline figure is compared with the
average day over the previous BASELINE_DAYS days.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.utils import timezone

from .inference.pipeline import local_day, local_tz
from .models import Aircraft, IngestRun, LandingCluster, Leg, Mission, Site
from .resolution import landing_rate

BASELINE_DAYS = 28
QUIET_DAYS = 7
LOOK_BACK_DAYS = 10  # how far back to look for a fully ingested day


def _window(day, days=1):
    tz = local_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, datetime.combine(day + timedelta(days=days), time.min, tzinfo=tz)


def summary_day(source, today=None):
    """(the latest fully ingested local day or None, whether the most recent download failed)."""
    today = today or timezone.localdate()
    runs = IngestRun.objects.filter(source=source)
    ok = set(runs.filter(status=IngestRun.OK).values_list("day", flat=True))
    latest = runs.order_by("-day", "-pk").first()
    failed = bool(latest and latest.status == IngestRun.FAILED)
    for back in range(1, LOOK_BACK_DAYS + 1):
        day = today - timedelta(days=back)
        if day in ok and day + timedelta(days=1) in ok:
            return day, failed
    return None, failed


@dataclass
class Change:
    """A day's figure against the average day before it."""
    value: float
    usual: float = None

    @property
    def percent(self):
        if not self.usual:
            return None
        return round(100 * (self.value - self.usual) / self.usual)

    @property
    def label(self):
        """'▲ 12% vs usual', '▼ 8% vs usual', 'about usual', or '' with no history to compare with."""
        p = self.percent
        if p is None:
            return ""
        if abs(p) < 3:
            return "about usual"
        return f"{'▲' if p > 0 else '▼'} {abs(p)}% vs usual"

    @property
    def usual_text(self):
        return "" if self.usual is None else f"usual {self.usual:.0f}"


@dataclass
class DailySummary:
    day: object = None
    failed: bool = False
    missions: Change = None
    by_label: dict = field(default_factory=dict)      # scene / interfacility / other -> count
    legs: Change = None
    rate: float = None                                 # resolved-landing rate that day (0-1)
    rate_change: float = None                          # in percentage points against the usual day

    @property
    def rate_label(self):
        if self.rate_change is None:
            return ""
        if abs(self.rate_change) < 1:
            return "about usual"
        return f"{'▲' if self.rate_change > 0 else '▼'} {abs(self.rate_change):.0f} pts vs usual"
    flew: int = 0
    fleet: int = 0
    busiest_aircraft: tuple = None                     # (aircraft, missions)
    busiest_hospital: tuple = None                     # (site, arrivals)
    top_route: tuple = None                            # (origin site or None, destination site or None, label, n)
    new_aircraft: list = field(default_factory=list)   # discovered that day (admins)
    new_spots: int = 0                                 # landing spots first used that day (admins)
    quiet: list = field(default_factory=list)          # [(aircraft, days since last seen)]
    lost_share: float = None                           # share of legs that ended with the signal lost


def daily_summary(source, statuses, today=None):
    day, failed = summary_day(source, today)
    s = DailySummary(day=day, failed=failed)
    if day is None:
        return s
    start, end = _window(day)
    base_start, _ = _window(day - timedelta(days=BASELINE_DAYS))

    missions = Mission.objects.filter(source=source, aircraft__status__in=statuses)
    legs = Leg.objects.filter(source=source, aircraft__status__in=statuses)
    today_missions = missions.filter(start_ts__gte=start, start_ts__lt=end)
    today_legs = legs.filter(start_ts__gte=start, start_ts__lt=end)

    def per_day(qs):
        days = Counter(local_day(ts) for ts in qs.filter(start_ts__gte=base_start, start_ts__lt=start)
                       .values_list("start_ts", flat=True))
        return sum(days.values()) / BASELINE_DAYS

    s.missions = Change(today_missions.count(), per_day(missions))
    labels = Counter(today_missions.values_list("label", flat=True))
    s.by_label = {"scene": labels["scene"], "interfacility": labels["interfacility"],
                  "other": sum(labels.values()) - labels["scene"] - labels["interfacility"]}
    s.legs = Change(today_legs.count(), per_day(legs))

    rate, _, total = landing_rate(today_legs, source)
    s.rate = rate
    if rate is not None:
        usual, _, _ = landing_rate(legs.filter(start_ts__gte=base_start, start_ts__lt=start), source)
        s.rate_change = None if usual is None else round(100 * (rate - usual), 1)
    if s.legs.value:
        s.lost_share = today_legs.filter(end_state=Leg.LOST).count() / s.legs.value

    s.flew = today_legs.values("aircraft").distinct().count()
    s.fleet = Aircraft.objects.filter(status__in=statuses, retired=False).count()

    by_aircraft = Counter(today_missions.values_list("aircraft", flat=True))
    if by_aircraft:
        pk, n = by_aircraft.most_common(1)[0]
        s.busiest_aircraft = (Aircraft.objects.get(pk=pk), n)
    arrivals = Counter(today_missions.filter(label__in=("scene", "interfacility"),
                                             destination_site__site_type=Site.HOSPITAL)
                       .values_list("destination_site", flat=True))
    if arrivals:
        pk, n = arrivals.most_common(1)[0]
        s.busiest_hospital = (Site.objects.get(pk=pk), n)
    routes = Counter(today_missions.exclude(label="reposition")
                     .values_list("origin_site", "destination_site", "label"))
    if routes:
        (origin, dest, label), n = routes.most_common(1)[0]
        sites = Site.objects.in_bulk([x for x in (origin, dest) if x])
        s.top_route = (sites.get(origin), sites.get(dest), dict(Mission.LABELS).get(label, label), n)

    s.new_aircraft = list(Aircraft.objects.filter(discovered_at__gte=start, discovered_at__lt=end,
                                                  status__in=Aircraft.VISIBLE))
    s.new_spots = LandingCluster.objects.filter(source=source, dismissed=False,
                                                first_seen__gte=start, first_seen__lt=end).count()
    quiet_before = end - timedelta(days=QUIET_DAYS)
    s.quiet = [(a, (end - a.last_seen).days) for a in Aircraft.objects.filter(
        status=Aircraft.CONFIRMED, retired=False, last_seen__lt=quiet_before).order_by("last_seen")[:5]]
    return s
