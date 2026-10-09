"""Run chores queued from the Operations page. Called by the background worker."""
import io
import tempfile
import traceback
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .inference import pipeline
from .ingest import run_backfill, utc_today
from .models import OpsTask, WorkerState
from .nasr import download_apt_base, extract_apt_base
from .registry import download_registry, load_registry
from .seed import seed_sites


def _logger(task):
    def log(line):
        task.log += f"{timezone.now():%H:%M:%S} {line}\n"
        task.save(update_fields=["log"])
        WorkerState.objects.filter(pk=1).update(heartbeat=timezone.now(), current=f"{task.get_kind_display()}: {line}"[:200])
    return log


def run_task(task: OpsTask):
    log = _logger(task)
    task.status, task.started_at = OpsTask.RUNNING, timezone.now()
    task.save(update_fields=["status", "started_at"])
    try:
        if task.kind == OpsTask.LOAD_REGISTRY:
            path = Path(tempfile.mkdtemp()) / "ReleasableAircraft.zip"
            log("downloading FAA registry (about 60 MB)…")
            download_registry(path)
            log(f"loaded {load_registry(path)} rotorcraft with Mode S codes")
        elif task.kind == OpsTask.SEED_SITES:
            if task.upload:
                log("using uploaded file")
                csv_bytes = extract_apt_base(bytes(task.upload))
            else:
                csv_bytes = download_apt_base(utc_today(), log=log)
            created, updated, skipped = seed_sites(io.BytesIO(csv_bytes))
            log(f"sites created {created}, updated {updated}, skipped {skipped} (already reviewed)")
        elif task.kind == OpsTask.BACKFILL:
            run_backfill(settings.BACKFILL_DAYS, log=log)
        elif task.kind == OpsTask.REBUILD:
            log(str(pipeline.rebuild(settings.PRIMARY_SOURCE)))
        elif task.kind == OpsTask.REFRESH:
            src = settings.PRIMARY_SOURCE
            log(f"clusters {pipeline.build_clusters(src)}, missions {pipeline.build_missions(src)}, "
                f"aggregates {pipeline.build_aggregates(src)}")
        task.status = OpsTask.OK
        log("done")
    except Exception as e:
        task.status = OpsTask.FAILED
        log(f"FAILED: {e}")
        task.log += traceback.format_exc()[-3000:]
    task.finished_at = timezone.now()
    task.upload = None  # don't keep uploaded files around
    task.save(update_fields=["status", "finished_at", "log", "upload"])
    WorkerState.objects.filter(pk=1).update(current="")
    return task.status


def queue_refresh(user=None):
    """Queue one refresh after review decisions; several quick decisions share one queued task."""
    if not OpsTask.objects.filter(kind=OpsTask.REFRESH, status=OpsTask.QUEUED).exists():
        OpsTask.objects.create(kind=OpsTask.REFRESH, requested_by=user)


def recover_interrupted():
    """After a worker restart: resume an interrupted backfill (it is resumable); fail anything else."""
    for task in OpsTask.objects.filter(status=OpsTask.RUNNING):
        task.log += f"{timezone.now():%H:%M:%S} worker restarted during this task\n"
        task.status = OpsTask.QUEUED if task.kind == OpsTask.BACKFILL else OpsTask.FAILED
        task.save(update_fields=["log", "status"])
