"""Background worker: the only long-running process besides the web server.

Every 30 s it records a heartbeat, runs the oldest queued Operations task, and once a day
(after NIGHTLY_UTC_HOUR:NIGHTLY_UTC_MINUTE) runs the nightly ingest. A failed nightly is retried with a decaying
rate (NIGHTLY_RETRY_FIRST_MIN, then doubling, at most NIGHTLY_RETRY_MAX times). A full rebuild queued by an
admin runs straight away; recounts and partial rebuilds run on their own shortly after the edits that
need them, and pending full rebuilds (plus a weekly one) overnight (tracker.rebuilds).
"""
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from tracker.ingest import process_rebuild_requests, run_nightly, utc_today
from tracker.models import OpsTask, RebuildRequest, WorkerState
from tracker.ops import recover_interrupted, run_task
from tracker.places import fill_site_places, suggest_pending
from tracker.rebuilds import process_overnight, process_quick


def next_retry_at(started, retries_done):
    """When the next nightly retry is due: NIGHTLY_RETRY_FIRST_MIN after the first failed run starts, then
    doubling (1 h, 2 h, 4 h, 8 h with the defaults)."""
    return started + timedelta(minutes=settings.NIGHTLY_RETRY_FIRST_MIN * 2 ** retries_done)


def tick(now=None, log=print):
    """One iteration of the worker loop. Returns what it did (for tests and logs)."""
    now = now or timezone.now()
    state = WorkerState.get()
    state.heartbeat = now
    state.save(update_fields=["heartbeat"])

    task = OpsTask.objects.filter(status=OpsTask.QUEUED).order_by("requested_at").first()
    if task:
        log(f"running task {task.pk} {task.kind}")
        return f"task:{task.kind}:{run_task(task)}"

    if RebuildRequest.objects.filter(processed_at__isnull=True).exists():
        WorkerState.objects.filter(pk=1).update(current="Rebuild")
        process_rebuild_requests()
        return "rebuild"

    quick = process_quick(now)
    if quick:
        return f"quick update: {quick}"

    overnight = process_overnight(now)
    if overnight:
        return overnight

    utc_now = now.astimezone(timezone.get_fixed_timezone(0))
    due = (utc_now.hour, utc_now.minute) >= (settings.NIGHTLY_UTC_HOUR, settings.NIGHTLY_UTC_MINUTE)
    if due and state.last_nightly != utc_today():
        WorkerState.objects.filter(pk=1).update(current="Nightly ingest")
        ok = run_nightly(log=log)
        retry_at = None if ok else next_retry_at(now, 0)
        WorkerState.objects.filter(pk=1).update(
            last_nightly=utc_today(), current="", nightly_retries=0, nightly_retry_at=retry_at)
        return "nightly" if ok else f"nightly failed; retry 1 at {retry_at:%H:%M} UTC"

    if state.nightly_retry_at and now >= state.nightly_retry_at:
        n = state.nightly_retries + 1
        WorkerState.objects.filter(pk=1).update(current=f"Nightly ingest (retry {n} of {settings.NIGHTLY_RETRY_MAX})")
        ok = run_nightly(log=log)
        retry_at = None if ok or n >= settings.NIGHTLY_RETRY_MAX else next_retry_at(now, n)
        WorkerState.objects.filter(pk=1).update(current="", nightly_retries=n, nightly_retry_at=retry_at)
        if ok:
            return f"nightly retry {n}: ok"
        if retry_at:
            return f"nightly retry {n} failed; retry {n + 1} at {retry_at:%H:%M} UTC"
        return f"nightly retry {n} failed; no more retries until the next nightly"

    # Spare time: suggest names for busy landing clusters (a few OpenStreetMap lookups per tick).
    looked_up = suggest_pending()
    looked_up += fill_site_places(max(0, settings.PLACE_LOOKUPS_PER_TICK - looked_up))
    return f"idle (place lookups: {looked_up})" if looked_up else "idle"


class Command(BaseCommand):
    help = "Run the background worker (nightly ingest + Operations page tasks). Runs forever."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single iteration and exit")

    def handle(self, *args, **opts):
        # On the very first deploy the web container may still be applying migrations.
        for attempt in range(20):
            try:
                recover_interrupted()
                break
            except Exception as e:
                self.stderr.write(f"waiting for the database to be ready ({e.__class__.__name__})")
                close_old_connections()
                time.sleep(15)
        self.stdout.write("worker started")
        while True:
            close_old_connections()
            try:
                did = tick(log=self.stdout.write)
                if did != "idle":
                    self.stdout.write(did)
            except Exception as e:  # keep the worker alive; the failure is visible in logs and the page
                self.stderr.write(f"worker error: {e}")
                did = "error"
            if opts["once"]:
                return
            if did.startswith("idle") or did == "error":
                time.sleep(30)
