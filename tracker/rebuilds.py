"""Running rebuilds, called by the background worker (tracker.rebuild_flag says what each kind is and when it is
needed). Every run is recorded with how long each step took, for the Operations page."""

import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import rawstore
from .inference import pipeline
from .models import PendingRebuild, RebuildRun, WorkerState
from .rebuild_flag import full_reasons, pending, rebuild_queued


def _timed(kind, reason, progress=None):
    """Start a RebuildRun. Returns (step, finish): `step(message)` reports progress (the banner) and times each step
    (the text before the first colon: "building legs: aircraft 3 of 9" is the "building legs" step)."""
    run = RebuildRun.objects.create(kind=kind, reason=reason[:300])
    current = {"name": None, "t": time.monotonic()}

    def close():
        if current["name"] is not None:
            run.steps.append([current["name"], round(time.monotonic() - current["t"], 1)])

    def step(message):
        name = message.split(":", 1)[0]
        if name != current["name"]:
            close()
            current["name"], current["t"] = name, time.monotonic()
        WorkerState.objects.filter(pk=1).update(heartbeat=timezone.now(), current=f"Rebuild: {message}"[:200])
        if progress:
            progress(message)

    def finish(ok=True, aircraft=None):
        close()
        current["name"] = None
        run.finished_at, run.ok, run.aircraft_rebuilt = timezone.now(), ok, aircraft
        run.save()
        WorkerState.objects.filter(pk=1).update(current="")

    return step, finish


def run_full(source, reason, progress=None):
    """Everything from the raw traces; applies whatever is pending and the current counting rules."""
    rebuild_queued()
    step, finish = _timed(PendingRebuild.FULL, reason, progress)
    try:
        stats = pipeline.rebuild(source, progress=step)
    except Exception:
        finish(ok=False)
        raise
    finish(aircraft=len(rawstore.hexes(source)))
    WorkerState.objects.filter(pk=1).update(last_full_rebuild=timezone.now())
    return stats


def process_quick(now=None, progress=None):
    """Apply pending recounts and partial rebuilds once QUICK_UPDATE_DELAY_S has passed since the last edit (so a
    burst of edits shares one run). A partial that would touch more than PARTIAL_MAX_SHARE of aircraft is left for
    the overnight full rebuild. Returns what it did, or None if nothing was due."""
    now = now or timezone.now()
    rows = list(pending().exclude(kind=PendingRebuild.FULL))
    if not rows or max(r.created_at for r in rows) > now - timedelta(seconds=settings.QUICK_UPDATE_DELAY_S):
        return None
    source = settings.PRIMARY_SOURCE
    partial = [r for r in rows if r.kind == PendingRebuild.PARTIAL]
    hexes = set()
    if partial:
        hexes = pipeline.hexes_near(source, [(r.lat, r.lon, r.radius_m) for r in partial])
        total = len(rawstore.hexes(source))
        if total and len(hexes) > settings.PARTIAL_MAX_SHARE * total:
            PendingRebuild.objects.filter(pk__in=[r.pk for r in partial]).update(kind=PendingRebuild.FULL)
            rows, partial, hexes = [r for r in rows if r.kind == PendingRebuild.RECOUNT], [], set()
            if not rows:
                return "partial too large: left for the overnight full rebuild"
    kind = PendingRebuild.PARTIAL if partial else PendingRebuild.RECOUNT
    reasons = list(dict.fromkeys(r.reason for r in reversed(rows)))
    step, finish = _timed(kind, "; ".join(reasons), progress)
    try:
        if partial:
            pipeline.partial_rebuild(source, hexes, progress=step)
        else:
            pipeline.build_downstream(source, step)
    except Exception:
        finish(ok=False)
        # Don't retry every tick: the overnight full rebuild applies these instead.
        PendingRebuild.objects.filter(pk__in=[r.pk for r in rows]).update(kind=PendingRebuild.FULL)
        raise
    finish(aircraft=len(hexes) if partial else 0)
    PendingRebuild.objects.filter(pk__in=[r.pk for r in rows]).update(processed_at=timezone.now())
    return f"{kind}: {len(hexes)} aircraft" if partial else kind


def process_overnight(now=None, progress=None):
    """Once a night from FULL_REBUILD_LOCAL_HOUR (local time, for up to 4 hours in case the worker was down): a full
    rebuild if one is pending, and every week on FULL_REBUILD_WEEKDAY as a safety net. Returns what it did, or None."""
    local = timezone.localtime(now or timezone.now())
    start = settings.FULL_REBUILD_LOCAL_HOUR
    if not start <= local.hour < start + 4 or WorkerState.get().last_overnight == local.date():
        return None
    WorkerState.objects.filter(pk=1).update(last_overnight=local.date())
    reason = full_reasons() or ("weekly full rebuild" if local.weekday() == settings.FULL_REBUILD_WEEKDAY else "")
    if not reason:
        return None
    run_full(settings.PRIMARY_SOURCE, reason, progress)
    return "overnight full rebuild"
