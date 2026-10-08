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


def process_rebuild_requests():
    pending = RebuildRequest.objects.filter(processed_at__isnull=True)
    sources = set(pending.values_list("source", flat=True))
    for source in sources:
        pipeline.rebuild(source)
    pending.update(processed_at=timezone.now())
    return sources
