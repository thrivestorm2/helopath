"""What is waiting to be rebuilt, and the admins' rebuild banner.

Edits record what they need (PendingRebuild):
- recount: discovery, clusters, missions, flows and coverage again without touching legs (a weather hangar, a
  service period, a site's type, reviewed flag or FAA ID);
- partial: legs re-derived only for aircraft with a leg starting or ending near a changed site, then a recount
  (a site added, moved, re-sized, re-elevated or deactivated);
- full: everything from the raw traces (FAA site seeding, a partial too big to be worth it, or a deploy with new
  counting rules: inference.pipeline.PIPELINE_VERSION newer than the last full rebuild).
Recounts and partials run on their own QUICK_UPDATE_DELAY_S after the last edit; full rebuilds run overnight, or
straight away from the banner's Run now or the Operations page (tracker.rebuilds). Edits that change no counting
(a site's name, city, notes, social pages) need nothing.
"""

import re

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .inference.pipeline import PIPELINE_VERSION
from .models import OpsTask, PendingRebuild, RebuildRequest, RebuildRun, WorkerState


def flag_recount(reason):
    PendingRebuild.objects.create(kind=PendingRebuild.RECOUNT, reason=reason[:200])


def flag_site(reason, *places):
    """A site changed at `places` [(lat, lon, radius_m)]: its old and new location when it moved."""
    for lat, lon, radius in places:
        PendingRebuild.objects.create(kind=PendingRebuild.PARTIAL, reason=reason[:200], lat=lat, lon=lon,
                                      radius_m=radius)


# Site fields that change how legs are classified, and ones that only change the recount; any other field (name,
# city, state, notes, social pages, origin) changes no figures.
SITE_LEG_FIELDS = {"lat", "lon", "radius_m", "elevation_ft", "is_active"}
SITE_RECOUNT_FIELDS = {"site_type", "reviewed", "weather_hangar", "faa_id"}
QUICK_NOTE = f" Figures update on their own in about {round(settings.QUICK_UPDATE_DELAY_S / 60)} minutes."


def flag_site_change(old, new, changed):
    """Record what saving a site needs: `old` is the site before the save (None when added), `changed` the names
    of the fields that changed. Returns True if anything was flagged."""
    if old is None:
        if new.is_active:
            flag_site(f"site {new.name} added", (new.lat, new.lon, new.radius_m))
        return new.is_active
    if changed & SITE_LEG_FIELDS:
        places = list(dict.fromkeys((s.lat, s.lon, s.radius_m) for s in (old, new) if s.is_active))
        if places:
            flag_site(f"site {new.name} edited", *places)
            return True
        return False  # inactive before and after: it never counted
    if changed & SITE_RECOUNT_FIELDS:
        flag_recount(f"site {new.name} edited")
        return True
    return False


def flag_full(reason):
    PendingRebuild.objects.create(kind=PendingRebuild.FULL, reason=reason[:200])


def pending():
    return PendingRebuild.objects.filter(processed_at__isnull=True)


def rebuild_queued():
    """A full rebuild was queued: everything pending so far, and the current counting rules, will be applied."""
    WorkerState.get()
    pending().update(processed_at=timezone.now())
    WorkerState.objects.filter(pk=1).update(rules_version=PIPELINE_VERSION)


def rebuild_active():
    """True while a rebuild of any kind is queued or running."""
    return (RebuildRequest.objects.filter(processed_at__isnull=True).exists()
            or OpsTask.objects.filter(Q(status=OpsTask.QUEUED) | Q(status=OpsTask.RUNNING),
                                      kind=OpsTask.REBUILD).exists()
            or RebuildRun.objects.filter(finished_at__isnull=True).exists())


def running_kind():
    run = RebuildRun.objects.filter(finished_at__isnull=True).first()
    return run.kind if run else PendingRebuild.FULL


def rebuild_progress():
    """What the running rebuild is doing (or that it is waiting for the worker), or None if none is active."""
    if not rebuild_active():
        return None
    current = WorkerState.get().current
    if current.lower().startswith("rebuild"):
        return current.split(":", 1)[1].strip() if ":" in current else "starting"
    return "queued; the worker starts it within a minute"


# How far through a rebuild each step starts, in percent. In a full rebuild building legs is most of the work;
# within it the percentage follows the aircraft count ("building legs: aircraft 239 of 682"). A partial rebuild
# re-derives a few aircraft, so the recount after it is most of its time.
STEP_PERCENT = {
    PendingRebuild.FULL: ([("clearing", 1), ("building legs", 2), ("widening", 86), ("discovering", 86),
                           ("finding landing clusters", 90), ("chaining missions", 93), ("totalling", 97),
                           ("mapping coverage", 99)], (2, 86)),
    PendingRebuild.PARTIAL: ([("building legs", 2), ("widening", 30), ("discovering", 35),
                              ("finding landing clusters", 55), ("chaining missions", 65), ("totalling", 85),
                              ("mapping coverage", 95)], (2, 30)),
    PendingRebuild.RECOUNT: ([("widening", 2), ("discovering", 10), ("finding landing clusters", 35),
                              ("chaining missions", 50), ("totalling", 80), ("mapping coverage", 92)], (2, 2)),
}
TITLES = {PendingRebuild.FULL: "Full rebuild", PendingRebuild.PARTIAL: "Quick update",
          PendingRebuild.RECOUNT: "Recount"}


def progress_percent(step, kind=PendingRebuild.FULL):
    """Rough percent complete for a rebuild step message (0 while queued)."""
    steps, (legs_start, legs_end) = STEP_PERCENT[kind]
    match = re.search(r"aircraft ([\d,]+) of ([\d,]+)", step)
    if match:
        done, total = (int(x.replace(",", "")) for x in match.groups())
        return round(legs_start + (legs_end - legs_start) * (done - 1) / max(total, 1))
    for prefix, percent in steps:
        if step.startswith(prefix):
            return percent
    return 0


def _reasons(rows):
    out = []
    for reason in rows.order_by("-created_at").values_list("reason", flat=True):
        if reason not in out:
            out.append(reason)
    return "; ".join(out)[:300]


def quick_reasons():
    """Edits waiting for the next recount or partial rebuild (it runs on its own), or ""."""
    return _reasons(pending().exclude(kind=PendingRebuild.FULL))


def full_reasons():
    """Why a full rebuild is needed (it runs overnight, or now from the banner), or ""."""
    reasons = _reasons(pending().filter(kind=PendingRebuild.FULL))
    if WorkerState.get().rules_version < PIPELINE_VERSION:
        reasons = "; ".join(r for r in ("the app was updated with new counting rules", reasons) if r)
    return reasons


def banner():
    """For the admins' banner:
    ("running", what it is doing, percent, title) while a rebuild of any kind runs or is queued;
    ("pending", edits the next quick update applies, why a full rebuild is due tonight) when something waits;
    None otherwise."""
    progress = rebuild_progress()
    if progress:
        kind = running_kind()
        return ("running", progress, progress_percent(progress, kind), TITLES[kind])
    quick, full = quick_reasons(), full_reasons()
    if quick or full:
        return ("pending", quick, full, round(settings.QUICK_UPDATE_DELAY_S / 60))
    return None
