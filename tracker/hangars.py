"""Weather-hangar suggestions: airports where a base's aircraft often start the day instead of at the base."""

from collections import Counter, defaultdict

from django.conf import settings
from django.db.models import Q

from .inference.pipeline import local_day
from .models import Aircraft, Leg, Site


def based_aircraft(site):
    """Visible aircraft based at the site: set by an admin, or inferred when no base was set."""
    return Aircraft.objects.filter(Q(base=site) | Q(base__isnull=True, inferred_base=site),
                                   status__in=Aircraft.VISIBLE)


def suggest_hangars(site, source, limit=5):
    """[(airport, days, aircraft count)] for airports where the base's aircraft started the local day at least
    HANGAR_SUGGEST_MIN_DAYS times, busiest first. The base itself is never suggested."""
    days, who = Counter(), defaultdict(set)
    for aircraft_id in based_aircraft(site).values_list("pk", flat=True):
        firsts = {}
        for ts, start_site in (Leg.objects.filter(source=source, aircraft_id=aircraft_id)
                               .order_by("start_ts").values_list("start_ts", "start_site_id")):
            firsts.setdefault(local_day(ts), start_site)
        for start_site in firsts.values():
            if start_site and start_site != site.pk:
                days[start_site] += 1
                who[start_site].add(aircraft_id)
    airports = Site.objects.in_bulk([pk for pk, n in days.items() if n >= settings.HANGAR_SUGGEST_MIN_DAYS])
    found = [(s, days[pk], len(who[pk])) for pk, s in airports.items() if s.site_type in (Site.AIRPORT, Site.BASE)]
    return sorted(found, key=lambda r: (-r[1], r[0].name))[:limit]
