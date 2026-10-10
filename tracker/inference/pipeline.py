"""Derived-table builds: legs -> discovery -> clusters -> missions -> aggregates.

Every function takes a `source` and touches only rows of that source (guardrail 2).
Everything here can be dropped and rebuilt from retained raw files (guardrail 3);
admin-owned Aircraft fields and cluster dismissals are never touched.
"""
import logging
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .. import rawstore
from ..geo import SiteIndex, haversine_m
from ..models import (
    Aircraft,
    DismissedClusterMemory,
    LandingCluster,
    Leg,
    Mission,
    MonthlyAggregate,
    RegistryAircraft,
    Site,
)
from ..rules import apply_auto_confirm_rules
from ..sources import get_adapter
from .legs import infer_legs, is_qualifying_visit

log = logging.getLogger("tracker.inference")


def local_tz():
    return ZoneInfo(settings.LOCAL_TZ)


def local_day(ts):
    return ts.astimezone(local_tz()).date()


def site_index():
    return SiteIndex(Site.objects.filter(is_active=True))


# --- legs ------------------------------------------------------------------------
def build_legs_for_hex(source, hexid, index, adapter=None):
    adapter = adapter or get_adapter(source)
    points = {}
    for data in rawstore.traces_for_hex(source, hexid):
        for p in adapter.parse(data):
            points.setdefault(p.ts_utc, p)  # de-duplicate overlap between day files
    ordered = [points[k] for k in sorted(points)]
    results = infer_legs(ordered, index)
    aircraft = Aircraft.objects.filter(icao_hex=hexid).first()
    with transaction.atomic():
        Leg.objects.filter(source=source, hex=hexid).delete()
        Leg.objects.bulk_create(
            [
                Leg(
                    hex=hexid,
                    aircraft=aircraft,
                    source=source,
                    start_ts=r.start_ts,
                    end_ts=r.end_ts,
                    start_lat=r.start_lat,
                    start_lon=r.start_lon,
                    end_lat=r.end_lat,
                    end_lon=r.end_lon,
                    start_site=r.start_site,
                    end_site=r.end_site,
                    end_state=r.end_state,
                    end_alt_ft=r.end_alt_ft,
                    confidence=r.confidence,
                    simplified_track=r.simplified_track,
                    start_on_ground=r.start_on_ground,
                    callsign=",".join(sorted(r.callsigns))[:50],
                )
                for r in results
            ],
            batch_size=500,
        )
    return len(results)


def build_legs(source, hexes=None):
    retained = rawstore.hexes(source)
    index = site_index()
    adapter = get_adapter(source)
    if hexes is None:
        targets = sorted(retained)
    else:
        targets = sorted(set(hexes) & retained)
        Leg.objects.filter(source=source, hex__in=set(hexes) - retained).delete()  # nothing retained any more
    total = 0
    for hexid in targets:
        total += build_legs_for_hex(source, hexid, index, adapter)
    return total


# --- discovery ---------------------------------------------------------------------
def _rule_window(visits):
    """First window of DISCOVERY_WINDOW_DAYS holding enough visits on enough days, or None."""
    window = timedelta(days=settings.DISCOVERY_WINDOW_DAYS)
    lo = 0
    for hi in range(len(visits)):
        while visits[hi].end_ts - visits[lo].end_ts > window:
            lo += 1
        chunk = visits[lo : hi + 1]
        days = {local_day(v.end_ts) for v in chunk}
        if len(chunk) >= settings.DISCOVERY_MIN_VISITS and len(days) >= settings.DISCOVERY_MIN_DAYS:
            return chunk
    return None


def _hints(source, hexid, legs):
    callsigns = Counter(c for leg in legs for c in leg.callsign.split(",") if c)
    meta = {}
    latest = rawstore.latest_for_hex(source, hexid)
    if latest is not None:
        try:
            meta = get_adapter(source).meta(latest)
        except Exception:  # hints are best-effort
            meta = {}
    return meta, ", ".join(c for c, _ in callsigns.most_common(3))


def run_discovery(source):
    """Create or refresh Aircraft rows. Writes only automation-owned fields."""
    now = timezone.now()
    legs = (
        Leg.objects.filter(source=source, end_site__site_type=Site.HOSPITAL)
        .select_related("end_site")
        .order_by("hex", "end_ts")
    )
    visits = defaultdict(list)
    for leg in legs:
        if is_qualifying_visit(leg):
            visits[leg.hex].append(leg)

    existing = {a.icao_hex: a for a in Aircraft.objects.all()}
    qualified = set()
    for hexid, vs in visits.items():
        fired = _rule_window(vs)
        if fired is None and hexid not in existing:
            continue
        aircraft = existing.get(hexid)
        all_legs = list(Leg.objects.filter(source=source, hex=hexid).only("callsign", "start_ts", "end_ts"))
        meta, callsigns = _hints(source, hexid, all_legs)
        reg = RegistryAircraft.objects.filter(mode_s_hex=hexid).first()
        if aircraft is None:
            tail = reg.tail_number if reg else (meta.get("r") or hexid.upper())
            if Aircraft.objects.filter(tail_number=tail).exists():
                tail = f"{tail}-{hexid}"[:10]
            aircraft = Aircraft(icao_hex=hexid, tail_number=tail, status=Aircraft.DISCOVERED, origin="auto",
                                discovered_at=now)
        if fired is not None:
            qualified.add(hexid)
            if aircraft.discovered_at is None:
                aircraft.discovered_at = now
            aircraft.discovery_rule = (
                f"{len(fired)} visits on {len({local_day(v.end_ts) for v in fired})} days within "
                f"{settings.DISCOVERY_WINDOW_DAYS} d (rule: >={settings.DISCOVERY_MIN_VISITS} visits, "
                f">={settings.DISCOVERY_MIN_DAYS} days, inferred conf >={settings.DISCOVERY_MIN_INFERRED_CONF})"
            )
        hospitals = Counter(v.end_site.name for v in vs)
        aircraft.qualifying_visits = len(vs)
        aircraft.distinct_hospitals = len(hospitals)
        aircraft.top_hospitals_hint = "; ".join(f"{n} ({c})" for n, c in hospitals.most_common(3))[:500]
        aircraft.first_seen = min((leg.start_ts for leg in all_legs), default=None)
        aircraft.last_seen = max((leg.end_ts for leg in all_legs), default=None)
        aircraft.discovery_source = source
        if reg:
            aircraft.make_model = f"{reg.make} {reg.model}".strip()[:100]
            aircraft.registrant_hint = reg.registrant_name[:200]
        elif meta.get("desc"):
            aircraft.make_model = meta["desc"][:100]
        aircraft.operator_hint = (meta.get("ownOp") or "")[:200]
        aircraft.callsign_hint = callsigns[:100]
        aircraft.save()
        existing[hexid] = aircraft

    # An auto-discovered row nobody has touched, which no longer meets the rule
    # (e.g. a site was deactivated), is a derived row and goes away on rebuild.
    for hexid, a in existing.items():
        untouched = a.status == Aircraft.DISCOVERED and a.origin == "auto" and not (
            a.operator_id or a.base_id or a.notes or a.program_name
        )
        if untouched and hexid not in qualified:
            a.delete()

    for a in Aircraft.objects.all():
        Leg.objects.filter(source=source, hex=a.icao_hex).exclude(aircraft=a).update(aircraft=a)
    apply_auto_confirm_rules()  # admin-defined rules confirm matching newly discovered aircraft
    infer_bases(source)
    return len(qualified)


def infer_bases(source):
    """Inferred base = where the aircraft most often starts its first leg of the local day."""
    for a in Aircraft.objects.all():
        firsts = {}
        for leg in Leg.objects.filter(source=source, aircraft=a).select_related("start_site").order_by("start_ts"):
            firsts.setdefault(local_day(leg.start_ts), leg)
        if not firsts:
            continue
        votes = Counter()
        order = {}
        coords = defaultdict(list)
        for leg in firsts.values():
            if leg.start_site_id:
                key = ("site", leg.start_site_id)
            else:
                key = ("pt", _cluster_key(leg.start_lat, leg.start_lon, coords))
            votes[key] += 1
            order.setdefault(key, len(order))
            coords[key].append((leg.start_lat, leg.start_lon))
        kind, ref = max(votes, key=lambda k: (votes[k], -order[k]))
        pts = coords[(kind, ref)]
        if kind == "site":
            site = Site.objects.get(pk=ref)
            a.inferred_base, a.inferred_base_lat, a.inferred_base_lon = site, site.lat, site.lon
        else:
            a.inferred_base = None
            a.inferred_base_lat = sum(p[0] for p in pts) / len(pts)
            a.inferred_base_lon = sum(p[1] for p in pts) / len(pts)
        a.save(update_fields=["inferred_base", "inferred_base_lat", "inferred_base_lon"])


def _cluster_key(lat, lon, coords):
    for key, pts in coords.items():
        if key[0] == "pt":
            clat = sum(p[0] for p in pts) / len(pts)
            clon = sum(p[1] for p in pts) / len(pts)
            if haversine_m(lat, lon, clat, clon) <= settings.CLUSTER_RADIUS_M:
                return key[1]
    return len([k for k in coords if k[0] == "pt"])


# --- landing clusters -----------------------------------------------------------------
def build_clusters(source):
    LandingCluster.objects.filter(source=source).delete()
    landings = Leg.objects.filter(
        source=source, end_state=Leg.LANDED, end_site__isnull=True, aircraft__status__in=Aircraft.VISIBLE
    ).order_by("end_ts", "id")
    clusters = []  # [sum_lat, sum_lon, n, aircraft set, first, last]
    for leg in landings:
        for c in clusters:
            if haversine_m(leg.end_lat, leg.end_lon, c[0] / c[2], c[1] / c[2]) <= settings.CLUSTER_RADIUS_M:
                c[0] += leg.end_lat
                c[1] += leg.end_lon
                c[2] += 1
                c[3].add(leg.aircraft_id)
                c[5] = leg.end_ts
                break
        else:
            clusters.append([leg.end_lat, leg.end_lon, 1, {leg.aircraft_id}, leg.end_ts, leg.end_ts])
    index = site_index()
    dismissed = list(DismissedClusterMemory.objects.all())
    rows = []
    for c in clusters:
        lat, lon = c[0] / c[2], c[1] / c[2]
        near = index.within(lat, lon, 50_000)
        rows.append(
            LandingCluster(
                source=source,
                lat=lat,
                lon=lon,
                landing_count=c[2],
                distinct_aircraft=len(c[3]),
                first_seen=c[4],
                last_seen=c[5],
                nearest_site=near[0][1] if near else None,
                nearest_site_distance_m=round(near[0][0]) if near else None,
                dismissed=any(haversine_m(lat, lon, d.lat, d.lon) <= settings.CLUSTER_RADIUS_M for d in dismissed),
            )
        )
    LandingCluster.objects.bulk_create(rows)
    return len(rows)


# --- missions -------------------------------------------------------------------------------
BASE, UNMATCHED, UNKNOWN = "BASE", "UNMATCHED", "UNKNOWN"


class _Base:
    def __init__(self, aircraft):
        site = aircraft.base or aircraft.inferred_base
        self.site = site
        if site:
            self.lat, self.lon, self.radius = site.lat, site.lon, site.radius_m
        elif aircraft.inferred_base_lat is not None:
            self.lat, self.lon = aircraft.inferred_base_lat, aircraft.inferred_base_lon
            self.radius = settings.BASE_POINT_RADIUS_M
        else:
            self.lat = None

    def contains(self, lat, lon, site_id=None):
        if self.lat is None:
            return False
        if self.site and site_id == self.site.pk:
            return True
        return haversine_m(lat, lon, self.lat, self.lon) <= self.radius


def _arrival(legs, i, base):
    leg = legs[i]
    nxt = legs[i + 1] if i + 1 < len(legs) else None
    if leg.end_state != Leg.LOST:
        if base.contains(leg.end_lat, leg.end_lon, leg.end_site_id):
            return BASE
        return leg.end_site if leg.end_site_id else UNMATCHED
    # Signal lost: bridge to the next leg's departure if it follows soon enough.
    bridge = timedelta(hours=settings.MISSION_LOST_SIGNAL_BRIDGE_HOURS)
    if nxt and nxt.start_ts - leg.end_ts <= bridge:
        if base.contains(nxt.start_lat, nxt.start_lon, nxt.start_site_id):
            return BASE
        if nxt.start_site_id:
            return nxt.start_site
        if nxt.start_on_ground:
            return UNMATCHED
    return UNKNOWN


def _symbol(stop):
    if stop in (UNMATCHED, UNKNOWN, BASE):
        return {UNMATCHED: "U", UNKNOWN: "?", BASE: "B"}[stop]
    return {Site.HOSPITAL: "H", Site.AIRPORT: "A", Site.BASE: "A"}.get(stop.site_type, "O")


def classify_mission(stops, stop_confs):
    """Return (label, origin, destination, confidence, rule)."""
    syms = [_symbol(s) for s in stops]
    rule = "base>" + "".join(f"{s}>" for s in syms) + "base"
    unknown_conf = settings.MISSION_UNKNOWN_CONF
    if syms == ["H", "H"] and stops[0].pk != stops[1].pk:
        return "interfacility", stops[0], stops[1], min(stop_confs), rule
    if syms == ["U", "H"]:
        return "scene", None, stops[1], stop_confs[1], rule
    if syms == ["H"]:
        return "single_hospital_unresolved", None, stops[0], stop_confs[0], rule
    if syms and all(s == "A" for s in syms):
        return "reposition", None, stops[-1], min(stop_confs), rule
    return "unknown", None, None, unknown_conf, rule


def build_missions(source):
    Mission.objects.filter(source=source).delete()
    max_gap = timedelta(hours=settings.MISSION_MAX_LEG_GAP_HOURS)
    created = 0
    for aircraft in Aircraft.objects.filter(status__in=Aircraft.VISIBLE).select_related("base", "inferred_base"):
        base = _Base(aircraft)
        if base.lat is None:
            continue
        legs = list(Leg.objects.filter(source=source, aircraft=aircraft).select_related("end_site", "start_site"))
        arrivals = [_arrival(legs, i, base) for i in range(len(legs))]
        groups = []  # (leg indexes, complete)
        cur = None
        prev_arr = None
        for i, leg in enumerate(legs):
            departs_base = base.contains(leg.start_lat, leg.start_lon, leg.start_site_id)
            if cur is not None and (departs_base or leg.start_ts - legs[i - 1].end_ts > max_gap):
                groups.append((cur, False))  # returned to base unseen, or lost the thread
                cur = None
            if cur is None:
                if not (departs_base or prev_arr == BASE):
                    prev_arr = arrivals[i]
                    continue
                cur = []
            cur.append(i)
            if arrivals[i] == BASE:
                groups.append((cur, True))
                cur = None
            prev_arr = arrivals[i]
        built = []  # (mission, leg indexes)
        for idxs, complete in groups:
            stops = [arrivals[i] for i in idxs[:-1]] if complete else [arrivals[i] for i in idxs]
            confs = [legs[i].confidence if legs[i].end_state != Leg.LOST else settings.MISSION_UNKNOWN_CONF
                     for i in (idxs[:-1] if complete else idxs)]
            if complete:
                label, origin, dest, conf, rule = classify_mission(stops, confs)
            else:
                label, origin, dest, conf = "unknown", None, None, settings.MISSION_UNKNOWN_CONF
                rule = "incomplete: base>" + "".join(f"{_symbol(s)}>" for s in stops) + "…"
            built.append((Mission(
                aircraft=aircraft,
                source=source,
                start_ts=legs[idxs[0]].start_ts,
                end_ts=legs[idxs[-1]].end_ts,
                label=label,
                origin_site=origin,
                destination_site=dest,
                confidence=round(conf, 3),
                rule_fired=rule,
            ), idxs))
        # Batched writes: one round trip per 500 rows instead of one per mission and per leg.
        Mission.objects.bulk_create([m for m, _ in built], batch_size=500)
        changed = []
        for m, idxs in built:
            for seq, i in enumerate(idxs):
                legs[i].mission, legs[i].mission_seq = m, seq
                changed.append(legs[i])
        Leg.objects.bulk_update(changed, ["mission", "mission_seq"], batch_size=500)
        created += len(built)
    return created


# --- aggregates -------------------------------------------------------------------------------
def build_aggregates(source):
    MonthlyAggregate.objects.filter(source=source).delete()
    rows = []
    for includes_unreviewed, statuses in ((True, Aircraft.VISIBLE), (False, (Aircraft.CONFIRMED,))):
        buckets = defaultdict(list)
        for m in Mission.objects.filter(source=source, aircraft__status__in=statuses).select_related("aircraft"):
            a = m.aircraft
            month = local_day(m.start_ts).replace(day=1)
            key = (a.operator_id, a.base_id or a.inferred_base_id, m.origin_site_id, m.destination_site_id, month, m.label)
            buckets[key].append(m.confidence)
        for (op, base, origin, dest, month, label), confs in buckets.items():
            rows.append(
                MonthlyAggregate(
                    source=source,
                    operator_id=op,
                    base_id=base,
                    origin_site_id=origin,
                    destination_site_id=dest,
                    month=month,
                    label=label,
                    count=len(confs),
                    mean_confidence=round(sum(confs) / len(confs), 3),
                    includes_unreviewed=includes_unreviewed,
                )
            )
    MonthlyAggregate.objects.bulk_create(rows)
    return len(rows)


def build_downstream(source):
    """Everything after legs. Cheap enough to run in full every night."""
    stats = {"discovered": run_discovery(source)}
    stats["clusters"] = build_clusters(source)
    stats["missions"] = build_missions(source)
    stats["aggregates"] = build_aggregates(source)
    return stats


def rebuild(source):
    """Drop and rebuild every derived table for one source from retained raw files."""
    with transaction.atomic():
        MonthlyAggregate.objects.filter(source=source).delete()
        Mission.objects.filter(source=source).delete()
        LandingCluster.objects.filter(source=source).delete()
        Leg.objects.filter(source=source).delete()
    stats = {"legs": build_legs(source)}
    if source == settings.PRIMARY_SOURCE:  # evaluation sources never create Aircraft rows
        stats.update(build_downstream(source))
    return stats


# --- retention ----------------------------------------------------------------------------------
def purge_undiscovered(source, today=None):
    """Guardrail 8: delete raw traces older than the retention window for hexes that are
    not discovered or confirmed aircraft, and the legs derived from them."""
    today = today or timezone.now().date()
    cutoff = today - timedelta(days=settings.UNDISCOVERED_RETENTION_DAYS)
    keep = set(Aircraft.objects.filter(status__in=Aircraft.VISIBLE).values_list("icao_hex", flat=True))
    removed = rawstore.purge_before(source, cutoff, keep)
    stale = Leg.objects.filter(source=source).exclude(hex__in=keep)
    cutoff_dt = datetime.combine(cutoff, time.min, tzinfo=dt_timezone.utc)
    hexes = set(stale.filter(start_ts__lt=cutoff_dt).values_list("hex", flat=True))
    if hexes:
        build_legs(source, hexes)  # re-derive from what is still retained
    return removed
