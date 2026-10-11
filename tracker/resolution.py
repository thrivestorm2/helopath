"""Why legs are not resolved to a site, and what would fix it (the Data quality "unresolved legs" page)."""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from django.conf import settings

from .geo import SiteIndex, haversine_m
from .models import Aircraft, LandingCluster, Leg, Site

NEAR_MISS_M = 2000          # a landing this close to a site (3 km for airports), but outside its radius, may just
                            # need a wider radius
HIGH_AGL_FT = 1500          # signal lost above this height is a coverage gap en route, not an approach
DEFAULT_GROUND_FT = 650     # ground elevation when no site is near (central Illinois)
MIN_CLUSTER_LANDINGS = 3    # a landing spot used this often probably deserves a site
MAX_SUGGESTED_RADIUS_M = 2000          # hospitals, bases and others
MAX_SUGGESTED_RADIUS_AIRPORT_M = 3000  # airports are big

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


NEAREST_SEARCH_M = 25_000   # how far to look for "nearest site" in the per-leg details


def distance_label(m):
    """'850 m', '3.4 km', or '> 25 km' when no site was found nearby."""
    if m is None:
        return f"> {NEAREST_SEARCH_M // 1000} km"
    return f"{m:,.0f} m" if m < 1000 else f"{m / 1000:.1f} km"


CATEGORY_TITLES = {key: title for key, title, _, _ in BUCKETS}
CATEGORY_ORDER = {key: i for i, (key, _, _, _) in enumerate(BUCKETS)}


@dataclass
class UnresolvedLeg:
    pk: int
    category: str
    end_state: str
    end_ts: object
    lat: float
    lon: float
    aircraft_id: int
    operator_id: int
    agl_ft: float = None
    nearest_site: object = None
    nearest_m: float = None

    @property
    def category_title(self):
        return CATEGORY_TITLES[self.category]

    @property
    def nearest_label(self):
        return distance_label(self.nearest_m)


@dataclass
class Diagnosis:
    total: int = 0
    resolved: int = 0
    counts: Counter = field(default_factory=Counter)
    near_sites: dict = field(default_factory=lambda: defaultdict(list))   # site -> [distance_m]
    bases: dict = field(default_factory=lambda: defaultdict(int))         # aircraft -> legs ending at its base
    spots: Counter = field(default_factory=Counter)                       # cluster -> legs
    rows: list = field(default_factory=list)                              # [UnresolvedLeg]

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
    """Sort a queryset of legs into Diagnosis buckets (only the unresolved ones are examined). Each unresolved
    leg is also kept as a row (UnresolvedLeg) for the per-category metrics and the leg list."""
    d = Diagnosis(total=legs.count())
    d.resolved = legs.filter(end_site__isnull=False).count()
    sites = SiteIndex(Site.objects.filter(is_active=True))
    clusters = SiteIndex(LandingCluster.objects.filter(source=source, dismissed=False,
                                                       landing_count__gte=MIN_CLUSTER_LANDINGS))
    bases = _point_bases()
    for pk, state, end_ts, lat, lon, alt, aircraft_id, operator_id in legs.filter(end_site__isnull=True).values_list(
            "pk", "end_state", "end_ts", "end_lat", "end_lon", "end_alt_ft", "aircraft_id", "aircraft__operator_id"):
        near = [(dist, s) for dist, s in sites.within(lat, lon, MAX_SUGGESTED_RADIUS_AIRPORT_M)
                if dist <= (MAX_SUGGESTED_RADIUS_AIRPORT_M if s.site_type == Site.AIRPORT else NEAR_MISS_M)]
        nearest = near[0] if near else next(iter(sites.within(lat, lon, NEAREST_SEARCH_M)), None)
        ground = near[0][1].elevation_ft if near and near[0][1].elevation_ft else DEFAULT_GROUND_FT
        base = bases.get(aircraft_id)
        if base and haversine_m(lat, lon, *base) <= settings.BASE_POINT_RADIUS_M:
            category = "base_not_site"
            d.bases[aircraft_id] += 1
        elif state == Leg.LOST and alt is not None and alt - ground > HIGH_AGL_FT:
            category = "lost_high"
        elif near:
            category = "near_site"
            d.near_sites[near[0][1]].append(near[0][0])
        elif spot := clusters.within(lat, lon, settings.CLUSTER_RADIUS_M * 2):
            category = "unmatched_spot"
            d.spots[spot[0][1]] += 1
        else:
            category = "lost_low" if state == Leg.LOST else "one_off_landing"
        d.counts[category] += 1
        d.rows.append(UnresolvedLeg(
            pk, category, state, end_ts, lat, lon, aircraft_id, operator_id,
            agl_ft=None if alt is None else max(0, alt - ground),
            nearest_site=nearest[1] if nearest else None, nearest_m=nearest[0] if nearest else None))
    return d


def category_metrics(d):
    """Per category: legs, share of all legs, aircraft involved, median distance to the nearest site, median
    height above ground at the end, and the operator with the most of them."""
    by_cat = defaultdict(list)
    for row in d.rows:
        by_cat[row.category].append(row)
    out = []
    for key, title, text, fixable in BUCKETS:
        rows = by_cat.get(key, [])
        distances = [r.nearest_m for r in rows if r.nearest_m is not None]
        heights = [r.agl_ft for r in rows if r.agl_ft is not None]
        operators = Counter(r.operator_id for r in rows if r.operator_id)
        out.append({
            "key": key, "title": title, "text": text, "fixable": fixable, "legs": len(rows),
            "share": len(rows) / d.total if d.total else 0,
            "aircraft": len({r.aircraft_id for r in rows}),
            "median_nearest_m": median(distances) if distances else None,
            "median_nearest": distance_label(median(distances) if distances else None) if rows else "—",
            "median_agl_ft": median(heights) if heights else None,
            "top_operator_id": operators.most_common(1)[0][0] if operators else None,
            "top_operator_legs": operators.most_common(1)[0][1] if operators else 0,
        })
    return out


def radius_suggestions(d, sites_index=None, min_legs=2, limit=25):
    # (limit=None: all of them)
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
        cap = MAX_SUGGESTED_RADIUS_AIRPORT_M if site.site_type == Site.AIRPORT else MAX_SUGGESTED_RADIUS_M
        suggested = min(cap, int(math.ceil(p80 / 50.0) * 50) + 50)
        neighbours = [dist for dist, s in index.within(site.lat, site.lon, 2 * cap) if s.pk != site.pk]
        nearest = neighbours[0] if neighbours else None
        if nearest is not None:
            suggested = min(suggested, int(nearest / 2 // 50 * 50))
        if suggested > site.radius_m:
            out.append((site, len(distances), suggested, nearest))
    return sorted(out, key=lambda r: (-r[1], r[0].name))[:limit]


def auto_radius_ok(site, suggested, nearest):
    """Widen automatically only when the suggestion stays small for the site type and below half the distance
    to the nearest other site (so it cannot start taking that site's landings)."""
    cap = settings.AUTO_RADIUS_MAX_AIRPORT_M if site.site_type == Site.AIRPORT else settings.AUTO_RADIUS_MAX_M
    return suggested <= cap and (nearest is None or suggested < nearest / 2)


def auto_expand_radii(source, days=90):
    """Widen the radius of sites with repeated landings just outside it, within auto_radius_ok's limits.
    Re-derives the legs of aircraft that landed in the newly covered ring so they count at once.
    Returns [(site, old radius, new radius, legs just outside)]."""
    from datetime import timedelta

    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from .inference.pipeline import build_legs
    from . import rawstore

    if source != settings.PRIMARY_SOURCE:
        return []
    legs = Leg.objects.filter(source=source, aircraft__status__in=Aircraft.VISIBLE,
                              start_ts__gte=timezone.now() - timedelta(days=days))
    changes = []
    for site, n, suggested, nearest in radius_suggestions(diagnose(legs, source), limit=None):
        if not auto_radius_ok(site, suggested, nearest):
            continue
        old = site.radius_m
        note = (f"Radius widened automatically from {old} m to {suggested} m: {n} landings just outside"
                + (f"; nearest other site {nearest:,.0f} m away." if nearest is not None else "."))
        site.radius_m = suggested
        site.notes = (site.notes + "\n" if site.notes else "") + note
        site.save(update_fields=["radius_m", "notes", "updated"])
        changes.append((site, old, suggested, n))
    if not changes:
        return []
    owner = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
    if owner:
        for site, old, new, n in changes:
            LogEntry.objects.log_actions(user_id=owner.pk, queryset=[site], action_flag=CHANGE, single_object=True,
                                         change_message=f"Radius widened automatically {old} m -> {new} m "
                                                        f"({n} landings just outside)")
    # Aircraft with unresolved landings now inside a widened radius: re-derive their legs.
    hexes = {h for h, lat, lon in Leg.objects.filter(source=source, end_site__isnull=True)
             .values_list("hex", "end_lat", "end_lon")
             if any(haversine_m(s.lat, s.lon, lat, lon) <= new for s, _, new, _ in changes)}
    build_legs(source, hexes & rawstore.hexes(source))
    return changes
