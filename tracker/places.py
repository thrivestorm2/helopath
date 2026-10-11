"""Suggest names for busy landing clusters from OpenStreetMap (Overpass API).

Only clusters with more than PLACE_LOOKUP_MIN_LANDINGS landings are looked up, a few per worker
tick, and answers are cached in PlaceSuggestion by rounded location. Map data © OpenStreetMap
contributors (ODbL).
"""
import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .geo import haversine_m
from .models import LandingCluster, PlaceSuggestion, Site

log = logging.getLogger("tracker.places")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "helopath/1.0 (air medical landing-site review; contact via app owner)"
US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


def location_key(lat, lon):
    """(lat3, lon3) Decimals: the cache key for a location (~100 m grid)."""
    q = Decimal("0.001")
    return (Decimal(str(lat)).quantize(q, ROUND_HALF_UP), Decimal(str(lon)).quantize(q, ROUND_HALF_UP))


def cached(lat, lon):
    lat3, lon3 = location_key(lat, lon)
    return PlaceSuggestion.objects.filter(lat3=lat3, lon3=lon3).first()


def overpass_query(lat, lon, radius_m):
    """Named hospitals/helipads nearby, then the city (admin level 8) and state (level 4) containing the point."""
    return (f"[out:json][timeout:25];("
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[amenity=hospital];"
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[healthcare=hospital];"
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[aeroway=helipad];"
            f");out center tags;"
            f"is_in({lat:.6f},{lon:.6f})->.a;"
            f'area.a[boundary=administrative][admin_level~"^(4|8)$"];out tags;')


def city_state_query(lat, lon):
    """Just the city (admin level 8) and state (level 4) containing the point."""
    return (f"[out:json][timeout:15];is_in({lat:.6f},{lon:.6f})->.a;"
            f'area.a[boundary=administrative][admin_level~"^(4|8)$"];out tags;')


def place_for_point(lat, lon, fetch=None):
    """(city, state code) for a point: from a cached cluster lookup at the same spot when there is one, else
    from OpenStreetMap. Either may be blank (e.g. outside any incorporated city). Raises on network errors."""
    s = cached(lat, lon)
    if s is not None and not s.error and (s.city or s.state):
        return s.city, s.state
    data = (fetch or fetch_overpass)(city_state_query(lat, lon))
    return containing_place(data.get("elements", []))


def state_code(value):
    """Two-letter code from 'IL', 'US-IL' or 'Illinois'; blank if unknown."""
    value = (value or "").strip()
    if value.upper().startswith("US-"):
        value = value[3:]
    if len(value) == 2 and value.upper() in US_STATES.values():
        return value.upper()
    return US_STATES.get(value.title(), US_STATES.get(value, ""))


def containing_place(elements):
    """(city, state code) from the administrative areas in an Overpass reply."""
    city = state = ""
    for e in elements:
        if e.get("type") != "area":
            continue
        tags = e.get("tags") or {}
        if tags.get("admin_level") == "8" and not city:
            city = (tags.get("name") or "")[:100]
        elif tags.get("admin_level") == "4" and not state:
            state = state_code(tags.get("ISO3166-2") or tags.get("name"))
    return city, state


def fetch_overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rank(elements, lat, lon):
    """Named hospitals first, then named helipads; nearest first. Returns candidate dicts.
    City/state come from the feature's own address tags, else from the area containing the cluster."""
    area_city, area_state = containing_place(elements)
    out, seen = [], set()
    for e in elements:
        tags = e.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        if tags.get("amenity") == "hospital" or tags.get("healthcare") == "hospital":
            kind = "hospital"
        elif tags.get("aeroway") == "helipad":
            kind = "helipad"
        else:
            continue
        plat = e.get("lat", (e.get("center") or {}).get("lat"))
        plon = e.get("lon", (e.get("center") or {}).get("lon"))
        if plat is None or plon is None or (name, kind) in seen:
            continue
        seen.add((name, kind))
        out.append({"name": name[:200], "kind": kind, "distance_m": round(haversine_m(lat, lon, plat, plon)),
                    "osm_url": f"https://www.openstreetmap.org/{e.get('type', 'node')}/{e.get('id')}",
                    "city": (tags.get("addr:city") or area_city)[:100],
                    "state": state_code(tags.get("addr:state")) or area_state})
    out.sort(key=lambda c: (c["kind"] != "hospital", c["distance_m"]))
    return out[:5]


def lookup(lat, lon, fetch=fetch_overpass):
    """Query and cache suggestions for one location. A failure is cached with its error and retried later."""
    lat3, lon3 = location_key(lat, lon)
    try:
        data = fetch(overpass_query(lat, lon, settings.PLACE_LOOKUP_RADIUS_M))
        candidates = rank(data.get("elements", []), lat, lon)
        best = candidates[0] if candidates else {}
        values = dict(name=best.get("name", ""), kind=best.get("kind", ""), distance_m=best.get("distance_m"),
                      osm_url=best.get("osm_url", ""), city=best.get("city", ""), state=best.get("state", ""),
                      candidates=candidates, error="")
    except Exception as e:  # network or parse problem: keep the worker running, try again later
        log.warning("place lookup failed at %s,%s: %s", lat, lon, e)
        values = dict(name="", kind="", distance_m=None, osm_url="", city="", state="", candidates=[],
                      error=str(e)[:200])
    suggestion, _ = PlaceSuggestion.objects.update_or_create(lat3=lat3, lon3=lon3, defaults=values)
    return suggestion


RETRY_FAILED_AFTER = timedelta(hours=1)


def pending_clusters(now=None):
    """Busy, undismissed clusters with no cached answer (or a failed one over an hour old), busiest first."""
    now = now or timezone.now()
    busy = LandingCluster.objects.filter(dismissed=False, landing_count__gt=settings.PLACE_LOOKUP_MIN_LANDINGS)
    for c in busy.order_by("-landing_count", "pk"):
        s = cached(c.lat, c.lon)
        if s is None or (s.error and now - s.looked_up_at > RETRY_FAILED_AFTER):
            yield c


def suggest_pending(max_lookups=None, fetch=fetch_overpass):
    """Look up a few pending clusters. Returns how many lookups were made."""
    n = 0
    limit = settings.PLACE_LOOKUPS_PER_TICK if max_lookups is None else max_lookups
    for cluster in pending_clusters():
        if n >= limit:
            break
        lookup(cluster.lat, cluster.lon, fetch=fetch)
        n += 1
    return n


_site_tries = {}  # site pk -> when its city/state was last looked up (this worker process)
RETRY_SITE_AFTER = timedelta(days=1)


def fill_site_places(max_lookups=None, fetch=None, now=None):
    """Fill blank city/state on active sites from their coordinates, a few per tick. Only blank fields are
    written, so nothing an admin typed is replaced. Returns how many sites were looked up."""
    now = now or timezone.now()
    limit = settings.PLACE_LOOKUPS_PER_TICK if max_lookups is None else max_lookups
    n = 0
    for site in Site.objects.filter(is_active=True).filter(models.Q(city="") | models.Q(state="")).order_by("-pk"):
        if n >= limit:
            break
        if now - _site_tries.get(site.pk, now - RETRY_SITE_AFTER) < RETRY_SITE_AFTER:
            continue
        _site_tries[site.pk] = now
        n += 1
        try:
            city, state = place_for_point(site.lat, site.lon, fetch=fetch)
        except Exception as e:  # keep the worker running; try again tomorrow
            log.warning("city/state lookup failed for site %s: %s", site.pk, e)
            continue
        values = {k: v for k, v in (("city", city), ("state", state)) if v and not getattr(site, k)}
        if values:
            Site.objects.filter(pk=site.pk).update(**values)  # no rebuild needed: city/state do not affect counts
    return n
