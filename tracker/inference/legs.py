"""Leg segmentation, end-state classification and confidence (spec section 11)."""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Optional

from django.conf import settings

from ..geo import SiteIndex, haversine_m
from ..models import Leg


@dataclass
class RawLeg:
    hex: str
    points: list                 # TracePoints, airborne points plus the touchdown/lift-off ground points
    ended_on_ground: bool
    started_on_ground: bool


@dataclass
class LegResult:
    hex: str
    start_ts: object
    end_ts: object
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    start_site: object
    end_site: object
    end_state: str
    end_alt_ft: Optional[float]
    confidence: float
    start_on_ground: bool = False
    simplified_track: list = field(default_factory=list)
    callsigns: set = field(default_factory=set)


def segment(points: Iterable) -> list[RawLeg]:
    """Split one aircraft's time-ordered points into airborne legs.

    A new leg starts when readsb's new-leg flag is set, when the gap between points
    exceeds LEG_GAP_MINUTES, or when the aircraft is on the ground. The readsb flag
    is only a hint; the gap rule handles a helicopter dropping below coverage.
    """
    gap = timedelta(minutes=settings.LEG_GAP_MINUTES)
    legs: list[RawLeg] = []
    cur: list = []
    last_ground = None
    prev = None

    def close(ended_on_ground, touchdown=None):
        nonlocal cur
        airborne = [p for p in cur if not p.on_ground]
        if len(airborne) >= settings.LEG_MIN_AIRBORNE_POINTS:
            pts = cur + ([touchdown] if touchdown is not None else [])
            if (pts[-1].ts_utc - pts[0].ts_utc).total_seconds() >= settings.LEG_MIN_DURATION_S:
                legs.append(RawLeg(cur[0].hex, pts, ended_on_ground, cur[0].on_ground))
        cur = []

    for p in points:
        if prev is not None and p.ts_utc - prev.ts_utc > gap:
            close(False)
            last_ground = None
        if p.on_ground:
            if cur:
                close(True, touchdown=p)
            last_ground = p
        else:
            if cur and p.new_leg:
                close(False)
            if not cur and last_ground is not None and p.ts_utc - last_ground.ts_utc <= gap:
                cur.append(last_ground)  # lift-off point gives the true departure location
            cur.append(p)
            last_ground = None
        prev = p
    close(False)
    return legs


def simplify(points, max_points):
    """Stride decimation that always keeps the endpoints."""
    if len(points) <= max_points:
        sel = points
    else:
        step = (len(points) - 1) / (max_points - 1)
        sel = [points[round(i * step)] for i in range(max_points)]
    return [
        [round(p.lat, 5), round(p.lon, 5), None if p.on_ground else p.alt_ft, int(p.ts_utc.timestamp())]
        for p in sel
    ]


def _neighbor_penalty(index: SiteIndex, site):
    others = [s for d, s in index.within(site.lat, site.lon, settings.CONF_NEIGHBOR_RADIUS_M) if s.pk != site.pk]
    return settings.CONF_NEIGHBOR_PENALTY if others else 0.0


def _agl(p, site):
    alt = p.alt_ft
    return None if alt is None else alt - site.elevation_ft


def _descending(pts):
    last = pts[-1]
    if last.vrate_fpm is not None:
        return last.vrate_fpm < 0
    window = [p for p in pts if (last.ts_utc - p.ts_utc).total_seconds() <= settings.INFERRED_DESCENT_LOOKBACK_S]
    alts = [p.alt_ft for p in window if p.alt_ft is not None]
    return len(alts) >= 2 and alts[-1] < alts[0]


def classify(raw: RawLeg, index: SiteIndex) -> LegResult:
    pts = raw.points
    first, last = pts[0], pts[-1]
    airborne = [p for p in pts if not p.on_ground]
    last_air = airborne[-1]

    # Departure site: the first point inside a site radius, low enough to be a lift-off.
    start_site = None
    hit = index.containing(first.lat, first.lon)
    if hit:
        agl = _agl(first, hit[1])
        if first.on_ground or agl is None or agl < settings.LANDED_MAX_AGL_FT * 2:
            start_site = hit[1]

    end_state, end_site, conf = Leg.LOST, None, 0.0
    hit = index.containing(last.lat, last.lon)
    if raw.ended_on_ground:
        end_state = Leg.LANDED  # observed touchdown; site may be unmatched (scene, base not in table)
        if hit:
            end_site = hit[1]
            conf = 1.0 - _neighbor_penalty(index, end_site)
    else:
        if hit:
            agl = _agl(last_air, hit[1])
            if agl is not None and agl < settings.LANDED_MAX_AGL_FT and (last_air.gs_kt or 0) < settings.LANDED_MAX_GS_KT:
                end_state, end_site = Leg.LANDED, hit[1]
                conf = 1.0 - _neighbor_penalty(index, end_site)
        if end_state == Leg.LOST:
            near = index.within(last_air.lat, last_air.lon, settings.INFERRED_MAX_DIST_M)
            if near and _descending(airborne):
                dist, site = near[0]
                agl = _agl(last_air, site)
                if agl is not None and agl < settings.INFERRED_MAX_AGL_FT:
                    end_state, end_site = Leg.INFERRED, site
                    conf = (
                        settings.CONF_INFERRED_CAP
                        - settings.CONF_DIST_PENALTY * dist / settings.INFERRED_MAX_DIST_M
                        - settings.CONF_ALT_PENALTY * max(agl, 0) / settings.INFERRED_MAX_AGL_FT
                        - _neighbor_penalty(index, site)
                    )
    end_alt = None if last.on_ground else last.alt_ft
    if end_alt is None and last.on_ground and end_site is not None:
        end_alt = end_site.elevation_ft
    return LegResult(
        hex=raw.hex,
        start_ts=first.ts_utc,
        end_ts=last.ts_utc,
        start_lat=first.lat,
        start_lon=first.lon,
        end_lat=last.lat,
        end_lon=last.lon,
        start_site=start_site,
        end_site=end_site,
        end_state=end_state,
        end_alt_ft=end_alt,
        confidence=round(max(0.0, min(1.0, conf)), 3),
        start_on_ground=first.on_ground,
        simplified_track=simplify(pts, settings.LEG_TRACK_MAX_POINTS),
        callsigns={p.callsign for p in pts if p.callsign},
    )


def infer_legs(points, index: SiteIndex) -> list[LegResult]:
    return [classify(r, index) for r in segment(points)]


def is_qualifying_visit(leg) -> bool:
    """Spec section 8: a leg ending at a hospital helipad, observed or confidently inferred."""
    site = leg.end_site
    if site is None or site.site_type != "hospital_helipad":
        return False
    if leg.end_state == Leg.LANDED:
        return True
    return leg.end_state == Leg.INFERRED and leg.confidence >= settings.DISCOVERY_MIN_INFERRED_CONF


__all__ = ["segment", "classify", "infer_legs", "is_qualifying_visit", "haversine_m"]
