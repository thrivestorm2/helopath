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
from django.utils import timezone

from .geo import haversine_m
from .models import LandingCluster, PlaceSuggestion

log = logging.getLogger("tracker.places")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "helopath/1.0 (air medical landing-site review; contact via app owner)"
HOSPITAL_WORDS = ("hospital", "medical", "health", "clinic", "memorial", "med ctr", "medical center")


def location_key(lat, lon):
    """(lat3, lon3) Decimals: the cache key for a location (~100 m grid)."""
    q = Decimal("0.001")
    return (Decimal(str(lat)).quantize(q, ROUND_HALF_UP), Decimal(str(lon)).quantize(q, ROUND_HALF_UP))


def cached(lat, lon):
    lat3, lon3 = location_key(lat, lon)
    return PlaceSuggestion.objects.filter(lat3=lat3, lon3=lon3).first()


def overpass_query(lat, lon, radius_m):
    return (f"[out:json][timeout:25];("
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[amenity=hospital];"
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[healthcare=hospital];"
            f"nwr(around:{radius_m},{lat:.6f},{lon:.6f})[aeroway=helipad];"
            f");out center tags;")


def fetch_overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rank(elements, lat, lon):
    """Named hospitals first, then named helipads; nearest first. Returns candidate dicts."""
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
                    "osm_url": f"https://www.openstreetmap.org/{e.get('type', 'node')}/{e.get('id')}"})
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
                      osm_url=best.get("osm_url", ""), candidates=candidates, error="")
    except Exception as e:  # network or parse problem: keep the worker running, try again later
        log.warning("place lookup failed at %s,%s: %s", lat, lon, e)
        values = dict(name="", kind="", distance_m=None, osm_url="", candidates=[], error=str(e)[:200])
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


def looks_like_hospital(suggestion):
    if suggestion.kind == "hospital":
        return True
    return any(w in suggestion.name.lower() for w in HOSPITAL_WORDS)
