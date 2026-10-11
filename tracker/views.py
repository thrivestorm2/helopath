import csv
import re
from urllib.parse import urlencode
from collections import Counter, defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .inference.pipeline import local_day
from . import places
from .geo import SiteIndex
from .hangars import based_aircraft, suggest_hangars
from .lookup import call_sign_suggestions, lookup_links, program_suggestions
from .models import (
    AutoConfirmRule,
    PlaceSuggestion,
    Aircraft,
    CoverageCell,
    FaaFacility,
    DismissedClusterMemory,
    IngestRun,
    LandingCluster,
    Leg,
    Mission,
    MonthlyAggregate,
    Operator,
    Program,
    RebuildRequest,
    Site,
)
from .operators import absorb_operator
from .ops import queue_refresh
from .resolution import CATEGORY_ORDER, CATEGORY_TITLES, category_metrics, diagnose, radius_suggestions
from .rebuild_flag import flag_rebuild, progress_percent, rebuild_active, rebuild_progress, rebuild_queued
from .roles import admin_required, is_admin
from .rules import apply_auto_confirm_rules

SOURCE = "adsblol"  # Phase 1 dashboards show one source; never mixed (guardrail 2)


def _confirmed_only(request):
    return request.GET.get("confirmed") == "1"


def _statuses(request):
    return (Aircraft.CONFIRMED,) if _confirmed_only(request) else Aircraft.VISIBLE


def _log(user, obj, message):
    LogEntry.objects.log_actions(
        user_id=user.pk, queryset=[obj], action_flag=CHANGE, change_message=message, single_object=True
    )


def _sorted(qs, request, allowed, default):
    sort = request.GET.get("sort", default)
    return qs.order_by(sort if sort.lstrip("-") in allowed else default), sort


def _filter_qs(request, drop=None):
    """Current query string without sort/page (and `drop`), for building sort links."""
    q = request.GET.copy()
    for key in ("sort", "page", drop):
        q.pop(key, None)
    return q.urlencode()


def _page(request, qs, per_page=50):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def medical_legs(request, days=None):
    qs = Leg.objects.filter(source=SOURCE, aircraft__status__in=_statuses(request))
    if days:
        qs = qs.filter(start_ts__gte=timezone.now() - timedelta(days=days))
    return qs


def resolved_rate(legs):
    total = legs.count()
    resolved = legs.filter(end_site__isnull=False).count()
    return (resolved / total if total else None), resolved, total


def dashboard(request):
    legs = medical_legs(request, days=90)
    rate, resolved, total = resolved_rate(legs)
    missions = Mission.objects.filter(source=SOURCE, aircraft__status__in=_statuses(request))
    legs_by_month = Counter(local_day(ts).strftime("%Y-%m") for ts in legs.values_list("start_ts", flat=True))
    missions_by_month = Counter(local_day(ts).strftime("%Y-%m") for ts in missions.values_list("start_ts", flat=True))
    months = sorted(set(legs_by_month) | set(missions_by_month))
    aggs = MonthlyAggregate.objects.filter(source=SOURCE, includes_unreviewed=not _confirmed_only(request))
    by_operator = defaultdict(int)  # (pk, name) -> missions; pk None means unassigned
    for a in aggs.select_related("operator"):
        by_operator[(a.operator_id, a.operator.name if a.operator else "(unassigned)")] += a.count
    # Hospitals receiving the most inferred patients (from scenes and from other hospitals).
    top_receiving = (aggs.filter(label__in=PATIENT_IN, destination_site__site_type=Site.HOSPITAL)
                     .values("destination_site", "destination_site__name")
                     .annotate(n=Sum("count"), scenes=Sum("count", filter=Q(label="scene")))
                     .order_by("-n", "destination_site__name")[:10])
    ok_runs = IngestRun.objects.filter(source=SOURCE, status=IngestRun.OK)
    return render(request, "tracker/dashboard.html", {
        "rate": rate, "resolved": resolved, "total": total,
        "months": [(m, legs_by_month[m], missions_by_month[m]) for m in months],
        "by_operator": [(pk, name, n, operator_link(request, pk))
                        for (pk, name), n in sorted(by_operator.items(), key=lambda kv: -kv[1])],
        "top_receiving": top_receiving,
        "aircraft_by_status": dict(Aircraft.objects.values_list("status").annotate(n=Count("id"))),
        "days_ingested": ok_runs.values("day").distinct().count(),
        "last_run": IngestRun.objects.filter(source=SOURCE).first(),
        "confirmed_only": _confirmed_only(request),
    })


def aircraft_list(request):
    qs = Aircraft.objects.filter(status__in=Aircraft.VISIBLE).select_related("operator", "program", "base", "inferred_base")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(tail_number__icontains=q) | Q(call_sign__icontains=q) | Q(icao_hex__icontains=q) | Q(make_model__icontains=q)
                       | Q(program__name__icontains=q))
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    if request.GET.get("operator") == "none":
        qs = qs.filter(operator__isnull=True)
    elif request.GET.get("operator"):
        qs = qs.filter(operator_id=request.GET["operator"])
    if request.GET.get("program") == "none":
        qs = qs.filter(program__isnull=True)
    elif request.GET.get("program"):
        qs = qs.filter(program_id=request.GET["program"])
    if request.GET.get("base"):
        qs = qs.filter(Q(base_id=request.GET["base"]) | Q(base__isnull=True, inferred_base_id=request.GET["base"]))
    landed_at = None
    if request.GET.get("site", "").isdigit():  # aircraft with a resolved arrival there, as counted on the site page
        landed_at = get_object_or_404(Site, pk=request.GET["site"])
        qs = qs.filter(pk__in=Leg.objects.filter(source=SOURCE, end_site=landed_at).values("aircraft_id"))
    qs, sort = _sorted(qs.annotate(name=Aircraft.sort_name()), request,
                       {"name", "icao_hex", "make_model", "status", "qualifying_visits", "first_seen", "last_seen",
                        "operator__name", "program__name"}, "name")
    bases = Site.objects.filter(Q(based_aircraft__isnull=False) | Q(inferred_based_aircraft__isnull=False)).distinct()
    return render(request, "tracker/aircraft_list.html", {
        "page": _page(request, qs), "sort": sort, "filter_qs": _filter_qs(request), "operators": Operator.objects.all(), "bases": bases,
        "programs": Program.objects.all(),
        "statuses": [s for s in Aircraft.STATUS_CHOICES if s[0] in Aircraft.VISIBLE],
        "landed_at": landed_at, "clear_site_qs": _filter_qs(request, drop="site"),
    })


def aircraft_detail(request, pk):
    aircraft = get_object_or_404(Aircraft, pk=pk, status__in=Aircraft.VISIBLE)
    legs = aircraft.legs.filter(source=SOURCE).select_related("start_site", "end_site").order_by("-start_ts")
    page = _page(request, legs, 25)
    # Weather hangar comes from the base: the one set on it, else the base's top suggestion (marked unconfirmed).
    base = aircraft.base or aircraft.inferred_base
    hangar, hangar_days = (base.weather_hangar if base else None), None
    if base and not hangar:  # (a base hangared at itself has weather_hangar == base: shown as "at base")
        top = suggest_hangars(base, SOURCE, limit=1)
        if top:
            hangar, hangar_days = top[0][0], top[0][1]
    # Most visited hospitals: resolved landings at hospital helipads, busiest first.
    most_visited = (legs.filter(end_site__site_type=Site.HOSPITAL).values("end_site", "end_site__name")
                    .annotate(n=Count("pk")).order_by("-n", "end_site__name")[:5])
    admin = is_admin(request.user)
    # Suggestions only until the value is set or an admin dismisses them.
    suggest_call_sign = admin and not aircraft.call_sign and not aircraft.call_sign_reviewed
    suggest_program = admin and not aircraft.program_id and not aircraft.program_reviewed
    return render(request, "tracker/aircraft_detail.html", {
        "lookup_links": lookup_links(aircraft),
        "suggest_call_sign": suggest_call_sign, "suggest_program": suggest_program,
        "call_sign_suggestions": call_sign_suggestions(aircraft, SOURCE) if suggest_call_sign else [],
        "program_suggestions": program_suggestions(aircraft) if suggest_program else [],
        "aircraft": aircraft, "page": page, "most_visited": most_visited, "base": base, "hangar": hangar, "hangar_days": hangar_days,
        "tracks": [{"id": leg.pk, "state": leg.end_state, "pts": [[p[0], p[1]] for p in leg.simplified_track]}
                   for leg in page],
    })


@require_POST
@admin_required
def aircraft_apply(request, pk):
    """One-tap "Use" for a suggested call sign or program on the aircraft page, or "Dismiss" to stop suggesting."""
    aircraft = get_object_or_404(Aircraft, pk=pk)
    field = request.POST.get("field")
    if request.POST.get("action") == "dismiss" and field in ("call_sign", "program"):
        setattr(aircraft, f"{field}_reviewed", True)
        aircraft.save(update_fields=[f"{field}_reviewed"])
        what = "call sign" if field == "call_sign" else "program"
        _log(request.user, aircraft, f"Dismissed {what} suggestions")
        messages.success(request, f"No more {what} suggestions for {aircraft}. You can still set one on the Edit page.")
    elif field == "call_sign":
        aircraft.call_sign = " ".join(request.POST.get("value", "").split())[:60]
        aircraft.save(update_fields=["call_sign"])
        _log(request.user, aircraft, f"Call sign set to {aircraft.call_sign or 'none'}")
        messages.success(request, f"Call sign set to {aircraft.call_sign}." if aircraft.call_sign else "Call sign cleared.")
    elif field == "program":
        aircraft.program = get_object_or_404(Program, pk=request.POST.get("value") or 0)
        had_operator = aircraft.operator_id
        aircraft.save(update_fields=["program"])
        also = f"; operator set to {aircraft.operator}" if aircraft.operator_id != had_operator else ""
        if also:
            queue_refresh(request.user)  # monthly totals are kept per operator
        _log(request.user, aircraft, f"Program set to {aircraft.program}{also}")
        messages.success(request, f"Program set to {aircraft.program}{also}.")
    return redirect(reverse("aircraft_detail", args=[aircraft.pk]) + "#lookup")


PATIENT_IN = ("interfacility", "scene")  # labels that bring a patient to their destination hospital


def _patient_aggs(request):
    return MonthlyAggregate.objects.filter(source=SOURCE, includes_unreviewed=not _confirmed_only(request))


def _with_patient_counts(request, qs):
    """Annotate sites with inferred patients received (scene or interfacility in) and sent (interfacility out)."""
    aggs = _patient_aggs(request)

    def total(site_field, **kw):
        sub = (aggs.filter(**{site_field: OuterRef("pk")}, **kw).order_by().values(site_field)
               .annotate(s=Sum("count")).values("s"))
        return Coalesce(Subquery(sub, output_field=IntegerField()), 0)

    return qs.annotate(received=total("destination_site", label__in=PATIENT_IN),
                       sent=total("origin_site", label="interfacility"))


def patient_flows(request, site, limit=25, show_months=6):
    """Inferred patients received at and sent from a site, per hospital on the other end (scenes are one row of the
    received table): the latest `show_months` months, the monthly average and the total over all months with data.
    Rows rank by total."""
    aggs = _patient_aggs(request)
    inbound = aggs.filter(destination_site=site, label__in=PATIENT_IN)
    outbound = aggs.filter(origin_site=site, label="interfacility")
    keep = "?confirmed=1" if _confirmed_only(request) else ""
    scene_map = route_map_url(request, None, site, "scene")
    received, sent = defaultdict(Counter), defaultdict(Counter)  # (name, link) -> month -> patients
    for label, pk, name, month, n in inbound.values_list("label", "origin_site", "origin_site__name", "month",
                                                          "count"):
        key = ("Scenes", scene_map) if label == "scene" else (name, reverse("site_detail", args=[pk]) + keep)
        received[key][month] += n
    for pk, name, month, n in outbound.values_list("destination_site", "destination_site__name", "month", "count"):
        sent[(name, reverse("site_detail", args=[pk]) + keep)][month] += n
    all_months = sorted({m for table in (received, sent) for c in table.values() for m in c})
    months = all_months[-show_months:]  # columns that fit on a phone; average and total use every month

    def rows(table):
        ranked = sorted(table.items(), key=lambda kv: (-sum(kv[1].values()), kv[0][0]))[:limit]
        return [(name, link, [c[m] for m in months], sum(c.values()) / len(all_months), sum(c.values()))
                for (name, link), c in ranked]

    total = lambda table: sum(sum(c.values()) for c in table.values())
    from_scenes = sum(received.get(("Scenes", scene_map), Counter()).values())
    unresolved = aggs.filter(destination_site=site, label="single_hospital_unresolved").aggregate(n=Sum("count"))["n"]
    return {
        "months": months, "n_months": len(all_months), "received_rows": rows(received), "sent_rows": rows(sent),
        "received": total(received), "from_scenes": from_scenes, "from_hospitals": total(received) - from_scenes,
        "sent": total(sent), "unresolved": unresolved or 0, "scene_map": scene_map,
    }


def site_list(request):
    qs = _with_patient_counts(request, Site.objects.filter(is_active=True))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(faa_id__icontains=q) | Q(city__icontains=q))
    if request.GET.get("type"):
        qs = qs.filter(site_type=request.GET["type"])
    if request.GET.get("state"):
        qs = qs.filter(state=request.GET["state"])
    qs, sort = _sorted(qs, request, {"name", "site_type", "city", "state", "faa_id", "radius_m", "reviewed",
                                     "received", "sent"}, "name")
    view = request.GET.get("view", "table")
    return render(request, "tracker/site_list.html", {
        "page": _page(request, qs, 100) if view == "table" else None,
        "map_sites": [{"id": s.pk, "name": s.name, "type": s.site_type, "lat": s.lat, "lon": s.lon, "r": s.radius_m}
                      for s in qs] if view == "map" else [],
        "view": view, "sort": sort, "filter_qs": _filter_qs(request), "types": Site.TYPE_CHOICES,
        "states": Site.objects.exclude(state="").values_list("state", flat=True).distinct().order_by("state"),
        "confirmed_only": _confirmed_only(request),
    })


def operator_link(request, operator_id, site=None):
    """Operator page, or for unassigned aircraft the aircraft list filtered to them (and to those that landed at
    `site`, when given); keeps the confirmed-only view."""
    if operator_id:
        return reverse("operator_detail", args=[operator_id]) + ("?confirmed=1" if _confirmed_only(request) else "")
    return (reverse("aircraft_list") + "?operator=none" + (f"&site={site.pk}" if site else "")
            + ("&status=confirmed" if _confirmed_only(request) else ""))


def site_detail(request, pk):
    site = get_object_or_404(Site, pk=pk)
    arrivals = Leg.objects.filter(source=SOURCE, end_site=site, aircraft__status__in=_statuses(request))
    table = defaultdict(Counter)  # (operator pk or None, name) -> month -> arrivals
    for op_id, op, ts in arrivals.values_list("aircraft__operator_id", "aircraft__operator__name", "end_ts"):
        table[(op_id, op or "(unassigned)")][local_day(ts).strftime("%Y-%m")] += 1
    months = sorted({m for c in table.values() for m in c})
    rows = []
    for (op_id, name), c in sorted(table.items(), key=lambda kv: (kv[0][0] is None, kv[0][1])):
        rows.append((op_id, name, operator_link(request, op_id, site), [c[m] for m in months], sum(c.values())))
    n_based = based_aircraft(site).count()
    return render(request, "tracker/site_detail.html", {
        "flows": patient_flows(request, site), "site": site, "months": months, "rows": rows, "n_based": n_based,
        # Once a hangar is set the header just names it; changing it is done on the admin Edit page.
        "hangar_suggestion": next(iter(suggest_hangars(site, SOURCE, limit=1)), None)
        if n_based and not site.weather_hangar_id else None,
        "total": arrivals.count(), "confirmed_only": _confirmed_only(request),
    })


def route_map_url(request, origin, destination, label, **scope):
    """Map of the scene / unknown landings on a route whose origin or destination is not a known site."""
    params = {"origin": getattr(origin, "pk", None) or "none", "dest": getattr(destination, "pk", None) or "none",
              "label": label, **{k: v if v is not None else "none" for k, v in scope.items()}}
    if _confirmed_only(request):
        params["confirmed"] = 1
    return reverse("route_map") + "?" + urlencode(params)


def _route_rows(request, flows, limit=15, **scope):
    """Busiest routes as ((origin, destination, label name), count, map link when an end is a scene/unknown)."""
    names = dict(Mission.LABELS)
    return [((o, d, names[label]), n, route_map_url(request, o, d, label, **scope) if o is None or d is None else None)
            for (o, d, label), n in flows.most_common(limit)]


def route_map(request):
    get = request.GET
    qs = Mission.objects.filter(source=SOURCE, aircraft__status__in=_statuses(request))
    for param, field in (("origin", "origin_site"), ("dest", "destination_site"), ("operator", "aircraft__operator"),
                         ("program", "aircraft__program")):
        value = get.get(param, "")
        if value == "none":
            qs = qs.filter(**{f"{field}__isnull": True})
        elif value.isdigit():
            qs = qs.filter(**{f"{field}_id": value})
    if get.get("label") in dict(Mission.LABELS):
        qs = qs.filter(label=get["label"])
    if get.get("base") == "none":
        qs = qs.filter(aircraft__base__isnull=True, aircraft__inferred_base__isnull=True)
    elif get.get("base", "").isdigit():
        qs = qs.filter(Q(aircraft__base_id=get["base"]) | Q(aircraft__base__isnull=True,
                                                             aircraft__inferred_base_id=get["base"]))
    month = get.get("month", "")
    missions = {m.pk: m for m in qs.select_related("aircraft")
                if not month or local_day(m.start_ts).strftime("%Y-%m") == month}
    # Each mission's landings away from known sites. A complete mission's last leg is the flight home, so it is
    # left out; where the signal was lost, the point is where the aircraft was last seen.
    legs = defaultdict(list)
    for leg in Leg.objects.filter(mission_id__in=list(missions)).order_by("mission_id", "mission_seq").only(
            "mission_id", "end_site_id", "end_lat", "end_lon", "end_state", "end_ts"):
        legs[leg.mission_id].append(leg)
    points = []
    for mid, mlegs in legs.items():
        m = missions[mid]
        for leg in mlegs if m.rule_fired.startswith("incomplete") else mlegs[:-1]:
            if leg.end_site_id is None:
                points.append({"lat": round(leg.end_lat, 5), "lon": round(leg.end_lon, 5),
                               "lost": leg.end_state == Leg.LOST, "day": local_day(leg.end_ts).isoformat(),
                               "tail": m.aircraft.display_name,
                               "url": reverse("aircraft_detail", args=[m.aircraft_id])})

    def pick(model, key):
        return model.objects.filter(pk=get[key]).first() if get.get(key, "").isdigit() else None

    scope = [pick(Operator, "operator"), pick(Program, "program"), pick(Site, "base")]
    return render(request, "tracker/route_map.html", {
        "points": points[:3000], "n_points": len(points), "n_missions": len(missions),
        "origin": pick(Site, "origin"), "destination": pick(Site, "dest"),
        "label": dict(Mission.LABELS).get(get.get("label"), "Any type"), "scope": [s for s in scope if s],
        "month": month, "confirmed_only": _confirmed_only(request),
    })


def program_detail(request, pk):
    program = get_object_or_404(Program.objects.select_related("operator"), pk=pk)
    can_delete = is_admin(request.user) and (request.user.is_superuser or not program.aircraft.exists())
    aircraft = (Aircraft.objects.filter(program=program, status__in=_statuses(request))
                .select_related("operator", "base", "inferred_base").order_by(Aircraft.sort_name()))
    missions = Mission.objects.filter(source=SOURCE, aircraft__program=program, aircraft__status__in=_statuses(request))
    labels = [code for code, _ in Mission.LABELS]
    per_month = defaultdict(Counter)
    flows = Counter()
    for m in missions.select_related("origin_site", "destination_site"):
        per_month[local_day(m.start_ts).replace(day=1)][m.label] += 1
        if m.origin_site_id or m.destination_site_id:
            flows[(m.origin_site, m.destination_site, m.label)] += 1
    months = sorted(per_month, reverse=True)
    return render(request, "tracker/program_detail.html", {
        "program": program, "can_delete": can_delete,
        "aircraft": aircraft,
        "labels": Mission.LABELS,
        "month_rows": [(m, [per_month[m][code] for code in labels], sum(per_month[m].values())) for m in months],
        "total": sum(sum(c.values()) for c in per_month.values()),
        "flows": _route_rows(request, flows, program=program.pk),
        "confirmed_only": _confirmed_only(request),
    })


@require_POST
@admin_required
def set_weather_hangar(request, pk):
    site = get_object_or_404(Site, pk=pk)
    hangar = get_object_or_404(Site, pk=request.POST["hangar"]) if request.POST.get("hangar") else None
    site.weather_hangar = hangar  # the site itself means "hangared at the base": no separate shelter
    site.save(update_fields=["weather_hangar", "updated"])
    what = "the base itself" if hangar == site else hangar.name if hangar else None
    _log(request.user, site, f"Weather hangar set to {what or 'none'}")
    flag_rebuild(f"weather hangar for {site.name}")
    messages.success(request, f"Weather hangar {f'set to {what}' if hangar else 'cleared'}. "
                              "Missions change after the next rebuild.")
    return redirect("site_detail", pk=site.pk)


@require_POST
@admin_required
def operator_acquired(request, pk):
    """Super admins: the operator was bought by another one; it becomes a program of the buyer."""
    if not request.user.is_superuser:
        raise PermissionDenied
    acquired = get_object_or_404(Operator, pk=pk)
    buyer = get_object_or_404(Operator, pk=request.POST.get("buyer") or 0)
    if buyer.pk == acquired.pk:
        messages.error(request, "Choose a different operator as the buyer.")
        return redirect("operator_detail", pk=acquired.pk)
    name = acquired.name
    program, moved = absorb_operator(acquired, buyer)
    _log(request.user, program, f"Operator {name} acquired by {buyer}: now a program of {buyer}, {moved} aircraft moved")
    _log(request.user, buyer, f"Acquired operator {name} ({moved} aircraft) as program {program}")
    queue_refresh(request.user)
    messages.success(request, f"{name} is now a program of {buyer}; {moved} aircraft moved. "
                              "Dashboard figures update within a minute.")
    return redirect("program_detail", pk=program.pk)


def operator_detail(request, pk):
    operator = get_object_or_404(Operator, pk=pk)
    aircraft = list(Aircraft.objects.filter(operator=operator, status__in=_statuses(request))
                    .select_related("program", "base", "inferred_base").order_by(Aircraft.sort_name()))
    # Aircraft grouped by program: the operator's programs (even with no aircraft shown) by name, then any other
    # program its aircraft carry, then those with none. Only grouped when there is a program to group by.
    groups = {p: [] for p in operator.programs.all()}
    for a in aircraft:
        groups.setdefault(a.program, []).append(a)
    named = sorted((p for p in groups if p), key=lambda p: p.name.lower())
    program_groups = [(p, groups[p]) for p in named] + ([(None, groups[None])] if groups.get(None) else [])
    aggs = MonthlyAggregate.objects.filter(source=SOURCE, operator=operator,
                                           includes_unreviewed=not _confirmed_only(request))
    labels = [code for code, _ in Mission.LABELS]
    per_month = defaultdict(Counter)
    flows, bases = Counter(), Counter()
    base_names = {}
    for a in aggs.select_related("origin_site", "destination_site", "base"):
        per_month[a.month][a.label] += a.count
        if a.origin_site_id or a.destination_site_id:
            flows[(a.origin_site, a.destination_site, a.label)] += a.count
        if a.base_id:
            bases[a.base_id] += a.count
            base_names[a.base_id] = a.base.name
    months = sorted(per_month, reverse=True)
    return render(request, "tracker/operator_detail.html", {
        "operator": operator,
        "buyers": Operator.objects.exclude(pk=operator.pk) if request.user.is_superuser else [],
        "aircraft": aircraft,
        "labels": Mission.LABELS,
        "month_rows": [(m, [per_month[m][code] for code in labels], sum(per_month[m].values())) for m in months],
        "total": sum(sum(c.values()) for c in per_month.values()),
        "flows": _route_rows(request, flows, operator=operator.pk),
        "bases": [(pk, base_names[pk], n) for pk, n in bases.most_common()],
        "program_groups": program_groups if named else [],
        "confirmed_only": _confirmed_only(request),
    })


def missions(request):
    qs = Mission.objects.filter(source=SOURCE, aircraft__status__in=_statuses(request)).select_related(
        "aircraft", "aircraft__operator", "origin_site", "destination_site")
    for param, field in (("label", "label"), ("aircraft", "aircraft_id"), ("operator", "aircraft__operator_id")):
        if request.GET.get(param):
            qs = qs.filter(**{field: request.GET[param]})
    if request.GET.get("min_conf"):
        qs = qs.filter(confidence__gte=float(request.GET["min_conf"]))
    return render(request, "tracker/missions.html", {
        "page": _page(request, qs), "labels": Mission.LABELS, "operators": Operator.objects.all(),
        "confirmed_only": _confirmed_only(request),
    })


def _flow_rows(request):
    qs = MonthlyAggregate.objects.filter(source=SOURCE, includes_unreviewed=not _confirmed_only(request)).select_related(
        "operator", "base", "origin_site", "destination_site")
    if request.GET.get("operator"):
        qs = qs.filter(operator_id=request.GET["operator"])
    if request.GET.get("month"):
        qs = qs.filter(month__startswith=request.GET["month"])
    if request.GET.get("label"):
        qs = qs.filter(label=request.GET["label"])
    return qs


def flows(request):
    return render(request, "tracker/flows.html", {
        "rows": _flow_rows(request)[:500], "operators": Operator.objects.all(), "labels": Mission.LABELS,
        "months": MonthlyAggregate.objects.filter(source=SOURCE).dates("month", "month", order="DESC"),
        "confirmed_only": _confirmed_only(request), "query": request.GET.urlencode(),
    })


def flows_csv(request):
    """The only export in the app: aggregate counts, never flight-level rows (guardrail 7)."""
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="helopath-flows.csv"'
    w = csv.writer(resp)
    w.writerow(["# Contains data from adsb.lol, made available under the Open Database License (ODbL). "
                "Missions are inferred; no patient information."])
    w.writerow(["source", "month", "operator", "base", "origin_site", "destination_site", "label", "count",
                "mean_confidence", "includes_unreviewed"])
    for r in _flow_rows(request):
        w.writerow([r.source, r.month.strftime("%Y-%m"), r.operator or "", r.base or "", r.origin_site or "",
                    r.destination_site or "", r.label, r.count, r.mean_confidence, r.includes_unreviewed])
    return resp


def _state_shares(groups):
    rows = []
    for name, counter in groups:
        total = sum(counter.values())
        rows.append((name, total, *[counter[s] / total if total else 0 for s, _ in Leg.END_STATES]))
    return sorted(rows, key=lambda r: -r[1])


def unresolved(request):
    """Why legs are not resolved to a site (90 days), with the fixes that would raise the rate."""
    d = diagnose(medical_legs(request, days=90), SOURCE)
    bases = []
    if d.bases:
        found = Aircraft.objects.in_bulk(list(d.bases))
        for pk, n in sorted(d.bases.items(), key=lambda kv: -kv[1])[:25]:
            a = found[pk]
            add = reverse("admin:tracker_site_add") + "?" + urlencode({
                "name": f"{a.program or a.operator or a.display_name} base", "site_type": Site.BASE,
                "lat": f"{a.inferred_base_lat:.6f}", "lon": f"{a.inferred_base_lon:.6f}",
                "radius_m": 300, "origin": "manual"})
            bases.append((a, n, add))
    # Every unresolved leg, filterable and sortable by category (and date, aircraft, distance, height).
    category = request.GET.get("cat", "")
    rows = [r for r in d.rows if not category or r.category == category]
    sorters = {
        "category": lambda r: (CATEGORY_ORDER[r.category], -r.end_ts.timestamp()),
        "date": lambda r: r.end_ts.timestamp(),
        "aircraft": lambda r: (r.aircraft_id or 0, -r.end_ts.timestamp()),
        "nearest": lambda r: (r.nearest_m is None, r.nearest_m or 0),
        "height": lambda r: (r.agl_ft is None, r.agl_ft or 0),
    }
    sort = request.GET.get("sort", "-date")
    key = sort.lstrip("-")
    if key not in sorters:
        sort, key = "-date", "date"
    rows.sort(key=sorters[key], reverse=sort.startswith("-"))
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    names = Aircraft.objects.in_bulk({r.aircraft_id for r in page if r.aircraft_id})
    for r in page:
        r.aircraft = names.get(r.aircraft_id)
    metrics = category_metrics(d)
    operators = Operator.objects.in_bulk({m["top_operator_id"] for m in metrics if m["top_operator_id"]})
    for m in metrics:
        m["top_operator"] = operators.get(m["top_operator_id"])
    return render(request, "tracker/unresolved.html", {
        "d": d, "metrics": metrics, "bases": bases, "radius_rows": radius_suggestions(d),
        "page": page, "cat": category, "sort": sort, "categories": list(CATEGORY_TITLES.items()),
        "cat_title": CATEGORY_TITLES.get(category, ""), "filter_qs": _filter_qs(request),
        "spots": [(c, n) for c, n in d.spots.most_common(15)],
        "confirmed_only": _confirmed_only(request),
    })


@require_POST
@admin_required
def set_site_radius(request, pk):
    """One-tap "widen radius" from the unresolved-legs page; takes effect after a rebuild."""
    site = get_object_or_404(Site, pk=pk)
    try:
        radius = int(request.POST["radius_m"])
    except (KeyError, ValueError):
        radius = 0
    if not 25 <= radius <= 3000:
        messages.error(request, "Radius must be between 25 and 3,000 m.")
    else:
        old = site.radius_m
        site.radius_m = radius
        site.save(update_fields=["radius_m", "updated"])
        _log(request.user, site, f"Radius {old} m -> {radius} m (unresolved legs page)")
        flag_rebuild(f"radius of {site.name}")
        messages.success(request, f"{site.name}: radius {old} m → {radius} m. Takes effect after the rebuild.")
    return redirect(reverse("unresolved") + "#radius")


def quality(request):
    legs = medical_legs(request, days=90)
    by_state = Counter(legs.values_list("end_state", flat=True))
    per_base, per_site = defaultdict(Counter), defaultdict(Counter)
    for state, base, ibase, site in legs.values_list("end_state", "aircraft__base__name",
                                                     "aircraft__inferred_base__name", "end_site__name"):
        per_base[base or ibase or "(no base site)"][state] += 1
        if site:
            per_site[site][state] += 1
    resolved_sites = set(Leg.objects.filter(source=SOURCE, end_site__isnull=False).values_list("end_site", flat=True))
    zero = Site.objects.filter(is_active=True, site_type=Site.HOSPITAL).exclude(pk__in=resolved_sites)
    rate, resolved, total = resolved_rate(legs)
    return render(request, "tracker/quality.html", {
        "rate": rate, "resolved": resolved, "total": total,
        "by_state": [(label, by_state[s], by_state[s] / total if total else 0) for s, label in Leg.END_STATES],
        "per_base": _state_shares(per_base.items()), "per_site": _state_shares(per_site.items())[:100],
        "end_states": Leg.END_STATES, "zero_sites": zero.order_by("state", "name"),
        "unreviewed_sites": Site.objects.filter(is_active=True, reviewed=False).count(),
        "gaps_rejoined": legs.aggregate(n=Sum("gaps_bridged"))["n"] or 0,
        "confirmed_only": _confirmed_only(request),
    })


def ingest_log(request):
    return render(request, "tracker/ingest_log.html", {"page": _page(request, IngestRun.objects.all(), 100)})


def _site_label(site):
    """Text for the shared site picker; the trailing #id is what the form submits back."""
    place = ", ".join(x for x in (site.city, site.state) if x)
    return f"{site.name}{' · ' + place if place else ''} #{site.pk}"


def _site_from_label(label):
    m = re.search(r"#(\d+)\s*$", label or "")
    return Site.objects.filter(pk=m[1]).first() if m else None


@admin_required
def review_queue(request):
    queue = (Aircraft.objects.filter(status=Aircraft.DISCOVERED)
             .select_related("inferred_base", "base", "operator", "program").order_by("-discovered_at", "pk"))
    page = Paginator(queue, 25).get_page(request.GET.get("page"))
    # Suggestions are rendered once per page (not once per card) to keep the page small on phones.
    hints = Aircraft.objects.exclude(operator_hint="").values_list("operator_hint", flat=True).distinct()
    operator_options = sorted(set(Operator.objects.values_list("name", flat=True)) | set(hints))
    sites = Site.objects.filter(is_active=True, site_type__in=[Site.BASE, Site.AIRPORT, Site.HOSPITAL]).order_by(
        "site_type", "name")
    rules = list(AutoConfirmRule.objects.filter(is_active=True).select_related("operator", "program"))
    for rule in rules:
        rule.confirmed_count = Aircraft.objects.filter(
            status=Aircraft.CONFIRMED, registrant_hint__icontains=rule.registrant_contains).count()
    return render(request, "tracker/review_queue.html", {
        "rules": rules,
        "operators": Operator.objects.all(),
        "page": page,
        "total": page.paginator.count,
        "operator_options": operator_options,
        "program_options": Program.objects.values_list("name", flat=True),
        "site_labels": [_site_label(s) for s in sites],
        "base_labels": {a.pk: _site_label(a.base) for a in page if a.base},
    })


@require_POST
@admin_required
def review_action(request, pk):
    aircraft = get_object_or_404(Aircraft, pk=pk)
    action = request.POST.get("action")
    if action in ("confirm", "reject"):
        aircraft.status = Aircraft.CONFIRMED if action == "confirm" else Aircraft.REJECTED
        aircraft.save(update_fields=["status"])
        _log(request.user, aircraft, f"Review: {action}")
        # Missions, clusters and aggregates are refreshed by the background worker, so this
        # click returns at once; several quick decisions share one queued refresh.
        queue_refresh(request.user)
        messages.success(request, f"{aircraft} {aircraft.status}. Dashboard figures update within a minute.")
    elif action == "operator":
        name = " ".join(request.POST.get("operator_name", "").split())[:200]
        op = Operator.objects.get_or_create(name=name)[0] if name else None
        aircraft.operator = op
        aircraft.save(update_fields=["operator"])
        _log(request.user, aircraft, f"Review: operator set to {op or 'none'}")
        messages.success(request, f"{aircraft}: operator {op or 'cleared'}.")
    elif action == "program":
        name = " ".join(request.POST.get("program_name", "").split())[:200]
        program = None
        if name:  # an existing program matches ignoring case; a new one is flown by this aircraft's operator
            program = Program.objects.filter(name__iexact=name).first() or Program.objects.create(
                name=name, operator=aircraft.operator)
        aircraft.program = program
        had_operator = aircraft.operator_id
        aircraft.save(update_fields=["program"])
        also = f"; operator set to {aircraft.operator}" if aircraft.operator_id != had_operator else ""
        if also:
            queue_refresh(request.user)
        _log(request.user, aircraft, f"Review: program set to {program or 'none'}{also}")
        messages.success(request, f"{aircraft}: program {program or 'cleared'}{also}.")
    elif action == "accept_base":
        aircraft.base = aircraft.inferred_base
        aircraft.save(update_fields=["base"])
        _log(request.user, aircraft, f"Review: accepted inferred base {aircraft.base}")
    elif action == "set_base":
        label = request.POST.get("base_label", "").strip()
        site = _site_from_label(label)
        if label and site is None:
            messages.error(request, "Pick the base from the suggestions list (it ends with #number).")
        else:
            aircraft.base = site
            aircraft.save(update_fields=["base"])
            _log(request.user, aircraft, f"Review: base set to {aircraft.base or 'none'}")
            messages.success(request, f"{aircraft}: base {aircraft.base or 'cleared'}.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/review/") else "review_queue")


@require_POST
@admin_required
def review_rules(request):
    """Add a rule, change a rule's operator, or switch a rule off. Rules must name an existing operator."""
    action = request.POST.get("action")
    nxt = request.POST.get("next", "")
    back = redirect(nxt if nxt.startswith("/review/") else "review_queue")
    if action in ("add", "set_operator"):
        operator = Operator.objects.filter(pk=request.POST.get("operator") or None).first()
        if operator is None:
            messages.error(request, "Choose an existing operator for the rule. New operators are added by typing "
                                    "a name in a card's Operator box and pressing Save.")
            return back
        if action == "add":
            text = " ".join(request.POST.get("registrant_contains", "").split())[:200]
            if len(text) < 4:
                messages.error(request, "Enter at least 4 characters of the registrant name.")
                return back
            rule, created = AutoConfirmRule.objects.get_or_create(
                registrant_contains=text, defaults={"operator": operator, "created_by": request.user})
            if not created:
                rule.is_active, rule.operator = True, operator
                rule.save(update_fields=["is_active", "operator"])
            _log(request.user, rule, f"Auto-confirm rule {'added' if created else 'updated'}: {text} -> {operator}")
        else:
            rule = get_object_or_404(AutoConfirmRule, pk=request.POST.get("rule"))
            rule.operator = operator
            rule.save(update_fields=["operator"])
            _log(request.user, rule, f"Auto-confirm rule operator set: {rule.registrant_contains} -> {operator}")
        n = apply_auto_confirm_rules()
        if n:
            queue_refresh(request.user)
        messages.success(request, f"Rule for “{rule.registrant_contains}” → {operator} saved. {n} aircraft updated "
                                  "now; future matches are confirmed automatically when discovered.")
    elif action == "off":
        rule = get_object_or_404(AutoConfirmRule, pk=request.POST.get("rule"))
        rule.is_active = False
        rule.save(update_fields=["is_active"])
        _log(request.user, rule, f"Auto-confirm rule switched off: {rule.registrant_contains}")
        messages.success(request, "Rule switched off. Aircraft it already confirmed stay confirmed.")
    return back


@admin_required
def cluster_queue(request):
    from django.conf import settings as dj_settings

    clusters = _open_clusters()  # most landings first
    busy = dj_settings.PLACE_LOOKUP_MIN_LANDINGS
    _add_site_links(clusters, next_url=reverse("cluster_queue"))  # saving the new site comes back here
    return render(request, "tracker/cluster_queue.html", {"clusters": clusters, "lookup_min": busy,
                                                          "recent_days": dj_settings.SPOT_MAP_DAYS})


def _cluster_add_site_url(c, name, city="", state="", site_type=Site.BASE, next_url=None):
    params = {"lat": f"{c.lat:.6f}", "lon": f"{c.lon:.6f}", "origin": "from_cluster", "radius_m": 300,
              "site_type": site_type, "name": name, "city": city, "state": state}
    if next_url:
        params["next"] = next_url
    return reverse("admin:tracker_site_add") + "?" + urlencode(params)


def _add_site_links(clusters, next_url=None):
    """Give each cluster `suggestion` (an OpenStreetMap name, if looked up), `create_url` (the admin's new-site
    form pre-filled for it) and `alternatives` [(candidate, url)]."""
    known = {(p.lat3, p.lon3): p for p in PlaceSuggestion.objects.all()}  # one query, not one per cluster
    busy = settings.PLACE_LOOKUP_MIN_LANDINGS
    for c in clusters:
        c.lookup_wanted = c.landing_count > busy
        c.suggestion = known.get(places.location_key(c.lat, c.lon)) if c.lookup_wanted else None
        if c.suggestion is not None and c.suggestion.name:
            # Choosing a suggested hospital or helipad makes the new site a hospital helipad.
            s = c.suggestion
            c.create_url = _cluster_add_site_url(c, s.name, s.city, s.state, Site.HOSPITAL, next_url)
            c.alternatives = [(cand, _cluster_add_site_url(c, cand["name"], cand.get("city", ""),
                                                           cand.get("state", ""), Site.HOSPITAL, next_url))
                              for cand in s.candidates[1:]]
        else:
            c.create_url = _cluster_add_site_url(c, f"Cluster {c.lat:.4f},{c.lon:.4f}", next_url=next_url)
            c.alternatives = []


@require_POST
@admin_required
def cluster_dismiss(request, pk):
    cluster = get_object_or_404(LandingCluster, pk=pk)
    cluster.dismissed = True
    cluster.save(update_fields=["dismissed"])
    DismissedClusterMemory.objects.create(lat=cluster.lat, lon=cluster.lon)
    LogEntry.objects.log_actions(
        user_id=request.user.pk, queryset=[cluster], action_flag=CHANGE,
        change_message=f"Dismissed landing cluster at {cluster.lat:.5f},{cluster.lon:.5f}", single_object=True)
    return redirect("cluster_queue")


@admin_required
def rebuild_status(request):
    """For the progress banner: {"active": bool, "step": what the rebuild is doing, "percent": 0-100}."""
    step = rebuild_progress()
    return JsonResponse({"active": step is not None, "step": step or "",
                         "percent": progress_percent(step) if step else 100})


@admin_required
def queue_rebuild(request):
    if request.method == "POST":
        if not rebuild_active():  # a second click, or another admin got there first: the banner already shows it
            RebuildRequest.objects.create(source=request.POST.get("source", SOURCE), requested_by=request.user)
        rebuild_queued()  # no message: the progress banner says it all
        nxt = request.POST.get("next", "")
        safe = nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure())
        return redirect(nxt if safe else "dashboard")
    return render(request, "tracker/queue_rebuild.html", {
        "pending": RebuildRequest.objects.filter(processed_at__isnull=True).select_related("requested_by"),
    })



@admin_required
def operations(request):
    from django.conf import settings as dj_settings

    from . import rawstore
    from .models import OpsTask, WorkerState

    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind not in dict(OpsTask.KINDS):
            messages.error(request, "Unknown task.")
        elif (OpsTask.objects.filter(kind=kind, status__in=[OpsTask.QUEUED, OpsTask.RUNNING]).exists()
              or (kind == OpsTask.REBUILD and rebuild_active())):
            messages.warning(request, "That task is already queued or running.")
        else:
            upload = request.FILES.get("upload")
            task = OpsTask.objects.create(kind=kind, requested_by=request.user,
                                          upload=upload.read() if upload and kind == OpsTask.SEED_SITES else None)
            if kind == OpsTask.REBUILD:
                rebuild_queued()
            _log(request.user, task, f"Queued task: {task.get_kind_display()}")
            messages.success(request, f"{task.get_kind_display()} queued. The worker picks it up within a minute.")
        return redirect("operations")

    state = WorkerState.get()
    age = (timezone.now() - state.heartbeat).total_seconds() if state.heartbeat else None
    days_target = dj_settings.BACKFILL_DAYS
    today = timezone.now().astimezone(timezone.get_fixed_timezone(0)).date()
    days_done = IngestRun.objects.filter(source=SOURCE, status=IngestRun.OK,
                                         day__gte=today - timedelta(days=days_target)).values("day").distinct().count()
    from .models import RegistryAircraft
    return render(request, "tracker/operations.html", {
        "state": state, "age": age, "alive": age is not None and (age < 120 or bool(state.current)),
        "tasks": OpsTask.objects.select_related("requested_by")[:15],
        "kinds": OpsTask.KINDS,
        "registry_count": RegistryAircraft.objects.count(),
        "site_count": Site.objects.filter(is_active=True).count(),
        "hospital_count": Site.objects.filter(is_active=True, site_type=Site.HOSPITAL).count(),
        "days_done": days_done, "days_target": days_target,
        "raw": rawstore.stats(SOURCE),
        "last_run": IngestRun.objects.filter(source=SOURCE).first(),
        "nightly_at": f"{dj_settings.NIGHTLY_UTC_HOUR:02d}:{dj_settings.NIGHTLY_UTC_MINUTE:02d} UTC",
    })


MAP_KINDS = ("base", "hospital", "hangar")
MAP_LAYERS = (*MAP_KINDS, "spot", "coverage")  # spots and coverage are off unless asked for


def site_map(request):
    """Full-page map of bases, hospitals and weather hangars, optionally for one operator and/or program."""
    get = request.GET
    operator = Operator.objects.filter(pk=get["operator"]).first() if get.get("operator", "").isdigit() else None
    program = Program.objects.filter(pk=get["program"]).first() if get.get("program", "").isdigit() else None
    filtered = bool(operator or program)
    aircraft = Aircraft.objects.filter(status__in=_statuses(request)).select_related("operator")
    legs = medical_legs(request, days=90)
    if operator:
        aircraft, legs = aircraft.filter(operator=operator), legs.filter(aircraft__operator=operator)
    if program:
        aircraft, legs = aircraft.filter(program=program), legs.filter(aircraft__program=program)

    based = defaultdict(list)  # site pk -> aircraft based there
    for a in aircraft.order_by(Aircraft.sort_name()):
        if a.base_id or a.inferred_base_id:
            based[a.base_id or a.inferred_base_id].append(a)
    base_q = Q(pk__in=list(based)) if filtered else Q(pk__in=list(based)) | Q(site_type=Site.BASE, is_active=True)
    bases = {s.pk: s for s in Site.objects.filter(base_q).select_related("weather_hangar")}
    hangar_for = defaultdict(list)  # hangar site pk -> bases it serves (other than itself)
    for s in bases.values():
        if s.weather_hangar_id and s.weather_hangar_id != s.pk:
            hangar_for[s.weather_hangar_id].append(s)
    hangars = {s.pk: s for s in Site.objects.filter(pk__in=list(hangar_for)).exclude(pk__in=list(bases))}
    landings = dict(legs.filter(end_site__site_type=Site.HOSPITAL).values_list("end_site").annotate(n=Count("pk")))
    hospital_q = Q(pk__in=list(landings)) if filtered else Q(site_type=Site.HOSPITAL, is_active=True)
    hospitals = Site.objects.filter(hospital_q).exclude(pk__in=[*bases, *hangars])

    def place(s):
        return ", ".join(p for p in (s.city, s.state) if p)

    def marker(kind, s, info):
        return {"kind": kind, "name": s.name, "lat": round(s.lat, 5), "lon": round(s.lon, 5),
                "url": reverse("site_detail", args=[s.pk]), "info": [i for i in [place(s), *info] if i]}

    def landings_line(s):
        n = landings.get(s.pk, 0)
        return f"{n:,} landing{'s' if n != 1 else ''} in the last 90 days" + (" by these aircraft" if filtered else "")

    markers = []
    for s in sorted(bases.values(), key=lambda s: s.name):
        crew = based.get(s.pk, [])
        names = [a.display_name for a in crew[:6]] + ([f"+{len(crew) - 6} more"] if len(crew) > 6 else [])
        operators = sorted({a.operator.name for a in crew if a.operator})
        # A hospital that is also a base is shown as a hospital, with its base details.
        kind = "hospital" if s.site_type == Site.HOSPITAL else "base"
        info = [f"{'Also a base' if kind == 'hospital' else 'Base'} · {len(crew)} aircraft"
                + (f": {', '.join(names)}" if names else ""), ", ".join(operators)]
        if kind == "hospital":
            info.insert(0, landings_line(s))
        elif s.site_type != Site.BASE:
            info.append(f"Also a {s.get_site_type_display().lower()}")
        if s.weather_hangar_id == s.pk:
            info.append("Hangar: at the base")
        elif s.weather_hangar_id:
            info.append(f"Weather hangar: {s.weather_hangar.name}")
        if hangar_for.get(s.pk):
            info.append("Also weather hangar for " + ", ".join(b.name for b in hangar_for[s.pk]))
        markers.append({**marker(kind, s, info), "base_too": kind == "hospital"})
    for s in sorted(hangars.values(), key=lambda s: s.name):
        markers.append(marker("hangar", s, ["Weather hangar for " + ", ".join(b.name for b in hangar_for[s.pk])]))
    for s in hospitals.order_by("name"):
        markers.append(marker("hospital", s, [landings_line(s)]))

    markers += _spot_markers(request)
    shown = [k for k in MAP_LAYERS if k in get.getlist("show")] if "f" in get else list(MAP_KINDS)
    counts = Counter(m["kind"] for m in markers)
    return render(request, "tracker/site_map.html", {
        "markers": markers, "shown": shown, "counts": [(k, counts[k]) for k in MAP_KINDS], "n_spots": counts["spot"],
        "operators": Operator.objects.order_by("name"), "programs": Program.objects.select_related("operator"),
        "operator": operator, "program": program, "confirmed_only": _confirmed_only(request),
        "coverage_days": settings.COVERAGE_DAYS,
    })


def _open_clusters():
    """Undismissed clusters not yet covered by an active site (one added since the last refresh), each with
    `recent_landings`: landings away from sites in the last SPOT_MAP_DAYS days, counted for the nearest
    cluster within the cluster radius (as the clusters were built). Most landings first."""
    sites = SiteIndex(Site.objects.filter(is_active=True))
    clusters = [c for c in LandingCluster.objects.filter(source=SOURCE, dismissed=False).select_related("nearest_site")
                if not any(d <= s.radius_m for d, s in sites.within(c.lat, c.lon, 2000))]
    recent = Counter()
    index = SiteIndex(clusters)
    for lat, lon in Leg.objects.filter(
            source=SOURCE, end_state=Leg.LANDED, end_site__isnull=True, aircraft__status__in=Aircraft.VISIBLE,
            end_ts__gte=timezone.now() - timedelta(days=settings.SPOT_MAP_DAYS)).values_list("end_lat", "end_lon"):
        near = index.within(lat, lon, settings.CLUSTER_RADIUS_M)
        if near:
            recent[near[0][1].pk] += 1
    for c in clusters:
        c.recent_landings = recent[c.pk]
    clusters.sort(key=lambda c: (-c.landing_count, -c.recent_landings, -c.last_seen.timestamp(), c.pk))
    return clusters


def _short_date(d):
    return f"{d:%b} {d.day}, {d.year}"


def _spot_markers(request):
    """Repeat landing spots that are not sites yet (the Cluster queue), for the map's "Unadded spots" layer.
    Admins get a link to the new-site form pre-filled for the spot, returning to the map after saving."""
    clusters = [c for c in _open_clusters() if c.recent_landings >= settings.SPOT_MAP_MIN_LANDINGS]
    admin = is_admin(request.user)
    if admin:
        _add_site_links(clusters, next_url=request.get_full_path())
    out = []
    for c in clusters:
        info = [f"Repeat landing spot, not a site yet",
                f"{c.recent_landings:,} landings in the last {settings.SPOT_MAP_DAYS} days "
                f"({c.landing_count:,} in all, by {c.distinct_aircraft} aircraft)",
                f"Last seen {_short_date(local_day(c.last_seen))}"]
        if c.nearest_site:
            info.append(f"Nearest site: {c.nearest_site.name} ({c.nearest_site_distance_m:,.0f} m)")
        suggestion = getattr(c, "suggestion", None)
        if suggestion is not None and suggestion.name:
            info.append(f"Looks like: {suggestion.name}" + (f", {suggestion.city}" if suggestion.city else ""))
        if admin:
            info.append("Click to add it as a site")
        out.append({"kind": "spot", "name": f"Spot {c.lat:.4f}, {c.lon:.4f}", "lat": round(c.lat, 5),
                    "lon": round(c.lon, 5), "url": c.create_url if admin else None, "info": info,
                    "link": "Add as site →"})
    return out


def find_faa_facility(code):
    """FaaFacility for an FAA ID as typed: case-insensitive; 'KPIA' style ICAO codes also match 'PIA'."""
    code = (code or "").strip().upper()
    if not code:
        return None
    hit = FaaFacility.objects.filter(faa_id=code).first()
    if hit is None and len(code) == 4 and code[0] in "KP":
        hit = FaaFacility.objects.filter(faa_id=code[1:]).first()
    return hit


@admin_required
def faa_facility(request):
    """Site form: details for a typed FAA ID, from the FAA airport/heliport data."""
    f = find_faa_facility(request.GET.get("id"))
    if f is None:
        loaded = FaaFacility.objects.exists()
        return JsonResponse({"found": False, "message": "No FAA facility with that ID." if loaded else
                             "FAA data is not loaded yet: run Seed FAA sites on the Operations page."})
    from .seed import default_radius
    return JsonResponse({"found": True, "faa_id": f.faa_id, "name": f.name, "site_type": f.site_type,
                         "city": f.city, "state": f.state, "lat": f.lat, "lon": f.lon,
                         "elevation_ft": f.elevation_ft, "radius_m": default_radius(f.site_type)})


@admin_required
def site_place(request):
    """City and state for a point, for the site form (fills them when the location is set)."""
    try:
        lat, lon = float(request.GET["lat"]), float(request.GET["lon"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "lat and lon are required"}, status=400)
    if request.GET.get("names"):
        # New-site form: up to three names to pick from, plus the city and state.
        try:
            names, (city, state) = places.site_name_options(lat, lon)
            return JsonResponse({"city": city, "state": state, "names": names, "error": ""})
        except Exception as e:
            reason = str(e)[:300]
        # Names unavailable: use a cluster lookup made earlier, if any, and still try for city/state.
        cached = places.cached(lat, lon)
        names = [{**c, "site_type": places.SITE_TYPE_FOR_KIND.get(c.get("kind"), Site.BASE)}
                 for c in (cached.candidates if cached else [])][:3]
        city, state = (cached.city, cached.state) if cached else ("", "")
        if not (city or state):
            try:
                city, state = places.place_for_point(lat, lon)
            except Exception:
                pass
        return JsonResponse({"city": city, "state": state, "names": names,
                             "error": "" if names else f"Nearby names unavailable: {reason}"})
    try:
        city, state = places.place_for_point(lat, lon)
    except Exception as e:
        return JsonResponse({"city": "", "state": "", "error": f"City/state lookup failed: {str(e)[:300]}"})
    return JsonResponse({"city": city, "state": state, "error": ""})


def coverage_data(request):
    """Grid cells where ADS-B positions were received, for the map's coverage layer (fetched when turned on)."""
    rows = list(CoverageCell.objects.filter(source=SOURCE).values_list("lat_i", "lon_i", "legs", "lowest_ft"))
    counts = sorted(r[2] for r in rows)
    # The colour scale tops out at the 95th percentile so a few very busy cells do not wash out the rest.
    top = counts[min(len(counts) - 1, int(0.95 * len(counts)))] if counts else 0
    return JsonResponse({"size": settings.COVERAGE_CELL_DEG, "top": top, "days": settings.COVERAGE_DAYS,
                         "cells": [[a, b, n, None if low is None else round(low)] for a, b, n, low in rows]})
