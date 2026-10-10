"""Leg segmentation, end-state classification and confidence (spec section 11)."""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Optional

from django.conf import settings

from ..geo import SiteIndex, angle_diff_deg, bearing_deg, haversine_m
from ..models import Leg


@dataclass
class RawLeg:
    hex: str
    points: list                 # TracePoints, airborne points plus the touchdown/lift-off ground points
    ended_on_ground: bool
    started_on_ground: bool
    gaps_bridged: int = 0        # coverage gaps rejoined by stitch()


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
    gaps_bridged: int = 0
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


KT_PER_MPS = 1.943844


def _motion(pts, at_start):
    """(median ground speed in kt, track in degrees) over a short run of airborne points, or Nones.
    Track is the reported one nearest the join (first point of a leg start, last of a leg end)
    when available, else the bearing travelled across the run."""
    speeds = sorted(p.gs_kt for p in pts if p.gs_kt is not None)
    gs = speeds[len(speeds) // 2] if speeds else None
    tracks = [p.track_deg for p in pts if p.track_deg is not None]
    if tracks:
        track = tracks[0] if at_start else tracks[-1]
    elif len(pts) >= 2 and haversine_m(pts[0].lat, pts[0].lon, pts[-1].lat, pts[-1].lon) > 200:
        track = bearing_deg(pts[0].lat, pts[0].lon, pts[-1].lat, pts[-1].lon)
    else:
        track = None
    return gs, track


def _end_window(leg, at_start):
    air = [p for p in leg.points if not p.on_ground]
    window = settings.STITCH_ENDPOINT_WINDOW_S
    if at_start:
        t0 = air[0].ts_utc
        return [p for p in air if (p.ts_utc - t0).total_seconds() <= window]
    t1 = air[-1].ts_utc
    return [p for p in air if (t1 - p.ts_utc).total_seconds() <= window]


def continues(a: RawLeg, b: RawLeg) -> bool:
    """Is leg b the same flight as leg a, picked up again after a coverage gap?

    Both pieces must be airborne at the join, the aircraft must be cruising at both ends, the gap
    short, and the jump across it consistent with the heading and speed flown on either side.
    """
    if a.ended_on_ground or b.started_on_ground:
        return False
    end_a, start_b = _end_window(a, at_start=False), _end_window(b, at_start=True)
    if not end_a or not start_b:
        return False
    pa, pb = end_a[-1], start_b[0]
    gap_s = (pb.ts_utc - pa.ts_utc).total_seconds()
    if gap_s <= 0 or gap_s > settings.STITCH_MAX_GAP_MINUTES * 60:
        return False
    gs_a, trk_a = _motion(end_a, at_start=False)
    gs_b, trk_b = _motion(start_b, at_start=True)
    if None in (gs_a, gs_b, trk_a, trk_b) or min(gs_a, gs_b) < settings.STITCH_MIN_GS_KT:
        return False
    dist_m = haversine_m(pa.lat, pa.lon, pb.lat, pb.lon)
    implied_kt = dist_m / gap_s * KT_PER_MPS
    cruise_kt = (gs_a + gs_b) / 2
    if not (settings.STITCH_SPEED_MIN_RATIO * cruise_kt <= implied_kt <= settings.STITCH_SPEED_MAX_RATIO * cruise_kt):
        return False
    limit = settings.STITCH_MAX_HEADING_DIFF_DEG
    if angle_diff_deg(trk_a, trk_b) > limit:
        return False
    if dist_m >= 300:  # the jump's own direction is only meaningful over some distance
        jump = bearing_deg(pa.lat, pa.lon, pb.lat, pb.lon)
        if angle_diff_deg(jump, trk_a) > limit or angle_diff_deg(jump, trk_b) > limit:
            return False
    return True


def stitch(legs: list[RawLeg]) -> list[RawLeg]:
    """Rejoin consecutive legs that are one flight split by a coverage gap (see continues())."""
    out: list[RawLeg] = []
    for leg in legs:
        if out and continues(out[-1], leg):
            prev = out[-1]
            out[-1] = RawLeg(prev.hex, prev.points + leg.points, leg.ended_on_ground, prev.started_on_ground,
                             prev.gaps_bridged + leg.gaps_bridged + 1)
        else:
            out.append(leg)
    return out


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


def _recent(pts, attr, window_s=60.0):
    """Most recent non-null value of attr within window_s of the last point (readsb often omits fields)."""
    last_ts = pts[-1].ts_utc
    for p in reversed(pts):
        if (last_ts - p.ts_utc).total_seconds() > window_s:
            break
        v = getattr(p, attr)
        if v is not None:
            return v
    return None


def _low_slow_site(airborne, index: SiteIndex):
    """Nearest site within LOW_SLOW_RADIUS_M if the aircraft was slow and low relative to it when lost.

    Speed is required: without it a low pass over a hospital would count as a landing.
    """
    last = airborne[-1]
    gs, alt = _recent(airborne, "gs_kt"), _recent(airborne, "alt_ft")
    if gs is None or alt is None or gs >= settings.LOW_SLOW_MAX_GS_KT:
        return None
    for dist, site in index.within(last.lat, last.lon, settings.LOW_SLOW_RADIUS_M):
        if alt - site.elevation_ft < settings.LOW_SLOW_MAX_AGL_FT:
            return dist, site
    return None


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
            low_slow = _low_slow_site(airborne, index)
            if low_slow:
                dist, site = low_slow
                end_state, end_site = Leg.INFERRED, site
                conf = (
                    settings.CONF_LOW_SLOW_CAP
                    - settings.CONF_LOW_SLOW_DIST_PENALTY * dist / settings.LOW_SLOW_RADIUS_M
                    - _neighbor_penalty(index, site)
                )
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
        gaps_bridged=raw.gaps_bridged,
        simplified_track=simplify(pts, settings.LEG_TRACK_MAX_POINTS),
        callsigns={p.callsign for p in pts if p.callsign},
    )


def infer_legs(points, index: SiteIndex) -> list[LegResult]:
    return [classify(r, index) for r in stitch(segment(points))]


def is_qualifying_visit(leg) -> bool:
    """Spec section 8: a leg ending at a hospital helipad, observed or confidently inferred."""
    site = leg.end_site
    if site is None or site.site_type != "hospital_helipad":
        return False
    if leg.end_state == Leg.LANDED:
        return True
    return leg.end_state == Leg.INFERRED and leg.confidence >= settings.DISCOVERY_MIN_INFERRED_CONF


__all__ = ["segment", "classify", "infer_legs", "is_qualifying_visit", "haversine_m"]
