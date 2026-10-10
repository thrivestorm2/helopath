import csv
import re
from urllib.parse import urlencode
from collections import Counter, defaultdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .inference.pipeline import local_day
from . import places
from .models import (
    AutoConfirmRule,
    PlaceSuggestion,
    Aircraft,
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
from .ops import queue_refresh
from .roles import admin_required
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
    by_base = defaultdict(int)
    for a in aggs.select_related("operator", "base"):
        by_operator[(a.operator_id, a.operator.name if a.operator else "(unassigned)")] += a.count
        by_base[(a.base_id, a.base.name if a.base else "(no base)")] += a.count
    ok_runs = IngestRun.objects.filter(source=SOURCE, status=IngestRun.OK)
    return render(request, "tracker/dashboard.html", {
        "rate": rate, "resolved": resolved, "total": total,
        "months": [(m, legs_by_month[m], missions_by_month[m]) for m in months],
        "by_operator": [(pk, name, n, operator_link(request, pk))
                        for (pk, name), n in sorted(by_operator.items(), key=lambda kv: -kv[1])],
        "by_base": [(pk, name, n) for (pk, name), n in sorted(by_base.items(), key=lambda kv: -kv[1])],
        "aircraft_by_status": dict(Aircraft.objects.values_list("status").annotate(n=Count("id"))),
        "days_ingested": ok_runs.values("day").distinct().count(),
        "last_run": IngestRun.objects.filter(source=SOURCE).first(),
        "confirmed_only": _confirmed_only(request),
    })


def aircraft_list(request):
    qs = Aircraft.objects.filter(status__in=Aircraft.VISIBLE).select_related("operator", "program", "base", "inferred_base")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(tail_number__icontains=q) | Q(icao_hex__icontains=q) | Q(make_model__icontains=q)
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
    qs, sort = _sorted(qs, request, {"tail_number", "icao_hex", "make_model", "status", "qualifying_visits",
                                     "first_seen", "last_seen", "operator__name", "program__name"}, "tail_number")
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
    return render(request, "tracker/aircraft_detail.html", {
        "aircraft": aircraft, "page": page,
        "tracks": [{"id": leg.pk, "state": leg.end_state, "pts": [[p[0], p[1]] for p in leg.simplified_track]}
                   for leg in page],
    })


def site_list(request):
    qs = Site.objects.filter(is_active=True)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(faa_id__icontains=q) | Q(city__icontains=q))
    if request.GET.get("type"):
        qs = qs.filter(site_type=request.GET["type"])
    if request.GET.get("state"):
        qs = qs.filter(state=request.GET["state"])
    qs, sort = _sorted(qs, request, {"name", "site_type", "city", "state", "faa_id", "radius_m", "reviewed"}, "name")
    view = request.GET.get("view", "table")
    return render(request, "tracker/site_list.html", {
        "page": _page(request, qs, 100) if view == "table" else None,
        "map_sites": [{"id": s.pk, "name": s.name, "type": s.site_type, "lat": s.lat, "lon": s.lon, "r": s.radius_m}
                      for s in qs] if view == "map" else [],
        "view": view, "sort": sort, "filter_qs": _filter_qs(request), "types": Site.TYPE_CHOICES,
        "states": Site.objects.exclude(state="").values_list("state", flat=True).distinct().order_by("state"),
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
    return render(request, "tracker/site_detail.html", {
        "site": site, "months": months, "rows": rows,
        "total": arrivals.count(), "confirmed_only": _confirmed_only(request),
    })


def program_detail(request, pk):
    program = get_object_or_404(Program.objects.select_related("operator"), pk=pk)
    aircraft = (Aircraft.objects.filter(program=program, status__in=_statuses(request))
                .select_related("operator", "base", "inferred_base").order_by("tail_number"))
    missions = Mission.objects.filter(source=SOURCE, aircraft__program=program, aircraft__status__in=_statuses(request))
    labels = [code for code, _ in Mission.LABELS]
    label_names = dict(Mission.LABELS)
    per_month = defaultdict(Counter)
    flows = Counter()
    for m in missions.select_related("origin_site", "destination_site"):
        per_month[local_day(m.start_ts).replace(day=1)][m.label] += 1
        if m.origin_site_id or m.destination_site_id:
            flows[(m.origin_site, m.destination_site, label_names[m.label])] += 1
    months = sorted(per_month, reverse=True)
    return render(request, "tracker/program_detail.html", {
        "program": program,
        "aircraft": aircraft,
        "labels": Mission.LABELS,
        "month_rows": [(m, [per_month[m][code] for code in labels], sum(per_month[m].values())) for m in months],
        "total": sum(sum(c.values()) for c in per_month.values()),
        "flows": flows.most_common(15),
        "confirmed_only": _confirmed_only(request),
    })


def operator_detail(request, pk):
    operator = get_object_or_404(Operator, pk=pk)
    aircraft = (Aircraft.objects.filter(operator=operator, status__in=_statuses(request))
                .select_related("base", "inferred_base").order_by("tail_number"))
    aggs = MonthlyAggregate.objects.filter(source=SOURCE, operator=operator,
                                           includes_unreviewed=not _confirmed_only(request))
    labels = [code for code, _ in Mission.LABELS]
    label_names = dict(Mission.LABELS)
    per_month = defaultdict(Counter)
    flows, bases = Counter(), Counter()
    base_names = {}
    for a in aggs.select_related("origin_site", "destination_site", "base"):
        per_month[a.month][a.label] += a.count
        if a.origin_site_id or a.destination_site_id:
            flows[(a.origin_site, a.destination_site, label_names[a.label])] += a.count
        if a.base_id:
            bases[a.base_id] += a.count
            base_names[a.base_id] = a.base.name
    months = sorted(per_month, reverse=True)
    return render(request, "tracker/operator_detail.html", {
        "operator": operator,
        "aircraft": aircraft,
        "labels": Mission.LABELS,
        "month_rows": [(m, [per_month[m][code] for code in labels], sum(per_month[m].values())) for m in months],
        "total": sum(sum(c.values()) for c in per_month.values()),
        "flows": flows.most_common(15),
        "bases": [(pk, base_names[pk], n) for pk, n in bases.most_common()],
        "programs": operator.programs.all(),
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
    rules = list(AutoConfirmRule.objects.filter(is_active=True).select_related("operator"))
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
        aircraft.save(update_fields=["program"])
        _log(request.user, aircraft, f"Review: program set to {program or 'none'}")
        messages.success(request, f"{aircraft}: program {program or 'cleared'}.")
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

    clusters = list(LandingCluster.objects.filter(source=SOURCE, dismissed=False).select_related("nearest_site"))
    known = {(p.lat3, p.lon3): p for p in PlaceSuggestion.objects.all()}  # one query, not one per cluster
    busy = dj_settings.PLACE_LOOKUP_MIN_LANDINGS
    def add_site_url(c, name, city="", state="", site_type=Site.BASE):
        return reverse("admin:tracker_site_add") + "?" + urlencode({
            "lat": f"{c.lat:.6f}", "lon": f"{c.lon:.6f}", "origin": "from_cluster", "radius_m": 300,
            "site_type": site_type, "name": name, "city": city, "state": state})

    for c in clusters:
        c.lookup_wanted = c.landing_count > busy
        c.suggestion = known.get(places.location_key(c.lat, c.lon)) if c.lookup_wanted else None
        if c.suggestion is not None and c.suggestion.name:
            # Choosing a suggested hospital or helipad makes the new site a hospital helipad.
            s = c.suggestion
            c.create_url = add_site_url(c, s.name, s.city, s.state, Site.HOSPITAL)
            c.alternatives = [(cand, add_site_url(c, cand["name"], cand.get("city", ""), cand.get("state", ""),
                                                  Site.HOSPITAL)) for cand in s.candidates[1:]]
        else:
            c.create_url = add_site_url(c, f"Cluster {c.lat:.4f},{c.lon:.4f}")
            c.alternatives = []
    return render(request, "tracker/cluster_queue.html", {"clusters": clusters, "lookup_min": busy})


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
def queue_rebuild(request):
    if request.method == "POST":
        RebuildRequest.objects.create(source=request.POST.get("source", SOURCE), requested_by=request.user)
        messages.success(request, "Rebuild queued; the background worker starts it within a minute.")
        return redirect("dashboard")
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
        elif OpsTask.objects.filter(kind=kind, status__in=[OpsTask.QUEUED, OpsTask.RUNNING]).exists():
            messages.warning(request, "That task is already queued or running.")
        else:
            upload = request.FILES.get("upload")
            task = OpsTask.objects.create(kind=kind, requested_by=request.user,
                                          upload=upload.read() if upload and kind == OpsTask.SEED_SITES else None)
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
