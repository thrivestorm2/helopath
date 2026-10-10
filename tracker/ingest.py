"""Nightly ingest orchestration (spec section 6)."""
import logging
import time
from datetime import date

from django.conf import settings
from django.utils import timezone

from .geo import BBox
from .inference import pipeline
from .models import IngestRun, RebuildRequest
from .registry import candidate_hexes
from .sources import get_adapter

log = logging.getLogger("tracker.ingest")


def ingest_day(day: date, source="adsblol", adapter=None, infer=True):
    adapter = adapter or get_adapter(source)
    run = IngestRun.objects.create(day=day, source=source, status=IngestRun.RUNNING)
    t0 = time.monotonic()
    try:
        candidates = candidate_hexes()
        if not candidates:
            raise RuntimeError("candidate pool is empty: run `manage.py load_registry` first")
        for _ in adapter.scan_day(day, candidates, BBox.from_settings()):
            pass
        st = adapter.stats
        run.release_used = st.release_used
        run.members_scanned = st.members_scanned
        run.candidates_kept = st.candidates_kept
        run.bytes_streamed = st.bytes_streamed
        run.bytes_retained = st.bytes_retained
        if infer:
            pipeline.build_legs(source, set(st.kept_hexes))
            if source == settings.PRIMARY_SOURCE:
                pipeline.build_downstream(source)
        run.status = IngestRun.OK
    except Exception as e:  # recorded, then re-raised so the timer unit shows failure
        run.status = IngestRun.FAILED
        run.error = f"{type(e).__name__}: {e}"
        log.exception("ingest %s %s failed", source, day)
        raise
    finally:
        run.duration_s = round(time.monotonic() - t0, 1)
        if run.status == IngestRun.FAILED and adapter.stats:
            run.members_scanned = adapter.stats.members_scanned
            run.bytes_streamed = adapter.stats.bytes_streamed
        run.save()
    return run


def day_done(day, source):
    return IngestRun.objects.filter(day=day, source=source, status=IngestRun.OK).exists()


def process_rebuild_requests(progress=None):
    pending = RebuildRequest.objects.filter(processed_at__isnull=True)
    sources = set(pending.values_list("source", flat=True))
    for source in sources:
        pipeline.rebuild(source, progress=progress)
    pending.update(processed_at=timezone.now())
    return sources


def utc_today():
    return timezone.now().astimezone(timezone.get_fixed_timezone(0)).date()


def run_backfill(days, source="adsblol", log=print):
    """Ingest the last `days` UTC days, skipping finished ones (resumable), then rebuild once."""
    from datetime import timedelta

    today = utc_today()
    failures = 0
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        done = days - back
        if day_done(day, source):
            continue
        log(f"[{done}/{days}] {day}: scanning…")
        try:
            # Inference runs once at the end; per day it would rebuild missions `days` times.
            run = ingest_day(day, source, infer=False)
            log(f"[{done + 1}/{days}] {day}: kept {run.candidates_kept} aircraft in {run.duration_s:.0f} s")
        except Exception as e:
            failures += 1
            log(f"[{done + 1}/{days}] {day}: FAILED {e}")
    log("rebuilding derived tables from retained traces…")
    log(str(pipeline.rebuild(source)))
    if failures:
        raise RuntimeError(f"{failures} day(s) failed; press Backfill again to retry them")


def run_nightly(source="adsblol", log=print):
    """Ingest any of the last 3 UTC days not yet done, process queued rebuilds, purge. True if all ok."""
    from datetime import timedelta

    today = utc_today()
    ok = True
    for back in (3, 2, 1):  # releases appear the following day; catch up on a missed night
        day = today - timedelta(days=back)
        if day_done(day, source):
            continue
        try:
            run = ingest_day(day, source)
            log(f"{day}: kept {run.candidates_kept} in {run.duration_s:.0f} s")
        except Exception as e:
            ok = False
            log(f"{day}: FAILED {e}")
    rebuilt = process_rebuild_requests()
    if rebuilt:
        log(f"rebuilt: {', '.join(sorted(rebuilt))}")
    log(f"purged {pipeline.purge_undiscovered(source, today)} undiscovered traces past retention")
    return ok
