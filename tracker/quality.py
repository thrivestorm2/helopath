"""ADS-B quality score per site: how well flights there are tracked (site page, Sites list, map).

Over the last QUALITY_DAYS, each event at a site scores
  landing seen (tracked to the ground)          1
  landing inferred (signal dropped coming down) 1/2
  departure first seen on the ground             1
  departure first seen already airborne          0
  signal lost within QUALITY_NEAR_M, not at a site (counted for the nearest site only)  0
and the score is the average, 0-100. Green is best, red worst.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from .geo import SiteIndex
from .models import Leg, Site

WEIGHTS = {"observed": 1.0, "inferred": 0.5, "dep_ground": 1.0, "dep_air": 0.0, "lost": 0.0}


@dataclass
class Quality:
    counts: Counter = field(default_factory=Counter)

    @property
    def events(self):
        return sum(self.counts.values())

    @property
    def score(self):
        """0-100, or None when there are too few events to say."""
        if self.events < settings.QUALITY_MIN_EVENTS:
            return None
        return round(100 * sum(WEIGHTS[k] * n for k, n in self.counts.items()) / self.events)

    @property
    def color(self):
        """Red (0) through amber to green (100); grey when there is not enough data."""
        return "#8a94a6" if self.score is None else f"hsl({round(1.2 * self.score)}, 70%, 38%)"

    @property
    def summary(self):
        c = self.counts
        return (f"{c['observed']} landings seen · {c['inferred']} inferred · {c['lost']} lost nearby · "
                f"{c['dep_ground']} departures seen on the ground · {c['dep_air']} first seen airborne")

    @property
    def label(self):
        return "not enough data" if self.score is None else str(self.score)


def site_quality(sites, statuses, source=None, days=None):
    """{site pk: Quality} for the given sites, counting legs of aircraft with these statuses."""
    source = source or settings.PRIMARY_SOURCE
    since = timezone.now() - timedelta(days=days or settings.QUALITY_DAYS)
    pks = [s.pk for s in sites]
    out = {pk: Quality() for pk in pks}
    legs = Leg.objects.filter(source=source, aircraft__status__in=statuses, start_ts__gte=since)
    state_key = {Leg.LANDED: "observed", Leg.INFERRED: "inferred", Leg.LOST: "lost"}
    for pk, state, n in (legs.filter(end_site_id__in=pks).values_list("end_site", "end_state")
                         .annotate(n=Count("pk"))):
        out[pk].counts[state_key.get(state, "inferred")] += n
    for pk, on_ground, n in (legs.filter(start_site_id__in=pks).values_list("start_site", "start_on_ground")
                             .annotate(n=Count("pk"))):
        out[pk].counts["dep_ground" if on_ground else "dep_air"] += n
    # Signal lost near a site but not at one: counts against the nearest active site within QUALITY_NEAR_M.
    index = SiteIndex(Site.objects.filter(is_active=True))
    for lat, lon in legs.filter(end_state=Leg.LOST, end_site__isnull=True).values_list("end_lat", "end_lon"):
        near = index.within(lat, lon, settings.QUALITY_NEAR_M)
        if near and near[0][1].pk in out:
            out[near[0][1].pk].counts["lost"] += 1
    return out
