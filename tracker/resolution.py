"""Why legs are not resolved to a site, and what would fix it (the Data quality "unresolved legs" page)."""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.conf import settings

from .geo import SiteIndex, haversine_m
from .models import Aircraft, LandingCluster, Leg, Site

NEAR_MISS_M = 2000          # a landing this close to a site, but outside its radius, may just need a wider radius
HIGH_AGL_FT = 1500          # signal lost above this height is a coverage gap en route, not an approach
DEFAULT_GROUND_FT = 650     # ground elevation when no site is near (central Illinois)
MIN_CLUSTER_LANDINGS = 3    # a landing spot used this often probably deserves a site
MAX_SUGGESTED_RADIUS_M = 2000

# (key, title, what it means, fixable here)
BUCKETS = [
    ("base_not_site", "Ended at the aircraft's base, which is not a site",
     "The aircraft's base is a location the tracker found, not a site, so every return home is unresolved. "
     "Adding the base as a site resolves all of them.", True),
    ("near_site", "Ended just outside a site's radius",
     f"Within {NEAR_MISS_M:,} m of a site but outside its landing radius. Widening the radius (or moving the "
     "site's pin) resolves them.", True),
    ("unmatched_spot", "Ended at a repeat landing spot with no site",
     f"A place used for {MIN_CLUSTER_LANDINGS}+ landings that is not a site yet: often a hospital pad or base "
     "missing from the FAA list. Create it from the Cluster queue.", True),
    ("one_off_landing", "Single landing away from any site",
     "Landed once somewhere that is not a site: most likely a scene response. Expected; a site would not help.",
     False),
    ("lost_low", "Signal lost low, away from sites",
     "Coverage ended near the ground away from any site (low terrain, a scene, or a pad outside the site list). "
     "Not fixable here; more receivers in the area would help.", False),
    ("lost_high", "Signal lost at altitude",
     f"Coverage ended more than {HIGH_AGL_FT:,} ft above the ground: a gap en route, not a landing. Not fixable "
     "here.", False),
]


@dataclass
class Diagnosis:
    total: int = 0
    resolved: int = 0
    counts: Counter = field(default_factory=Counter)
    near_sites: dict = field(default_factory=lambda: defaultdict(list))   # site -> [distance_m]
    bases: dict = field(default_factory=lambda: defaultdict(int))         # aircraft -> legs ending at its base
    spots: Counter = field(default_factory=Counter)                       # cluster -> legs

    @property
    def rate(self):
        return self.resolved / self.total if self.total else None

    @property
    def fixable(self):
        return sum(self.counts[key] for key, _, _, fixable in BUCKETS if fixable)

    @property
    def best_rate(self):
        """The rate if every fixable leg were resolved."""
        return (self.resolved + self.fixable) / self.total if self.total else None

    def buckets(self):
        return [(key, title, text, fixable, self.counts[key], self.counts[key] / self.total if self.total else 0)
                for key, title, text, fixable in BUCKETS]


def _point_bases():
    """{aircraft id: (lat, lon)} for aircraft whose base is a found location, not a site."""
    return {pk: (lat, lon) for pk, lat, lon in Aircraft.objects.filter(
        status__in=Aircraft.VISIBLE, base__isnull=True, inferred_base__isnull=True,
        inferred_base_lat__isnull=False).values_list("pk", "inferred_base_lat", "inferred_base_lon")}


def diagnose(legs, source):
    """Sort a queryset of legs into Diagnosis buckets (only the unresolved ones are examined)."""
    d = Diagnosis(total=legs.count())
    d.resolved = legs.filter(end_site__isnull=False).count()
    sites = SiteIndex(Site.objects.filter(is_active=True))
    clusters = SiteIndex(LandingCluster.objects.filter(source=source, dismissed=False,
                                                       landing_count__gte=MIN_CLUSTER_LANDINGS))
    bases = _point_bases()
    for state, lat, lon, alt, aircraft_id in legs.filter(end_site__isnull=True).values_list(
            "end_state", "end_lat", "end_lon", "end_alt_ft", "aircraft_id"):
        near = sites.within(lat, lon, NEAR_MISS_M)
        base = bases.get(aircraft_id)
        if base and haversine_m(lat, lon, *base) <= settings.BASE_POINT_RADIUS_M:
            d.counts["base_not_site"] += 1
            d.bases[aircraft_id] += 1
            continue
        ground = near[0][1].elevation_ft if near and near[0][1].elevation_ft else DEFAULT_GROUND_FT
        if state == Leg.LOST and alt is not None and alt - ground > HIGH_AGL_FT:
            d.counts["lost_high"] += 1
            continue
        if near:
            d.counts["near_site"] += 1
            d.near_sites[near[0][1]].append(near[0][0])
            continue
        spot = clusters.within(lat, lon, settings.CLUSTER_RADIUS_M * 2)
        if spot:
            d.counts["unmatched_spot"] += 1
            d.spots[spot[0][1]] += 1
            continue
        d.counts["lost_low" if state == Leg.LOST else "one_off_landing"] += 1
    return d


def radius_suggestions(d, sites_index=None, min_legs=2, limit=25):
    """[(site, legs just outside, suggested radius, nearest other site distance)] busiest first.

    The suggestion covers 80% of the near misses, rounded up to 50 m, but stops short of half the distance to
    the nearest other site so one site does not start taking another's landings."""
    index = sites_index or SiteIndex(Site.objects.filter(is_active=True))
    out = []
    for site, distances in d.near_sites.items():
        if len(distances) < min_legs:
            continue
        distances.sort()
        p80 = distances[min(len(distances) - 1, math.ceil(0.8 * len(distances)) - 1)]
        suggested = min(MAX_SUGGESTED_RADIUS_M, int(math.ceil(p80 / 50.0) * 50) + 50)
        neighbours = [dist for dist, s in index.within(site.lat, site.lon, 2 * MAX_SUGGESTED_RADIUS_M) if s.pk != site.pk]
        nearest = neighbours[0] if neighbours else None
        if nearest is not None:
            suggested = min(suggested, int(nearest / 2 // 50 * 50))
        if suggested > site.radius_m:
            out.append((site, len(distances), suggested, nearest))
    return sorted(out, key=lambda r: (-r[1], r[0].name))[:limit]
