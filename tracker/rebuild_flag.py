"""'A rebuild is needed' banner for admins.

Shown when an edit that only takes effect after a rebuild was saved (sites, weather hangars), or when a deploy
brought new counting rules (inference.pipeline.PIPELINE_VERSION is newer than the last rebuild queued). Cleared
when a rebuild is queued; hidden while one is already waiting or running.
"""

from django.db.models import Q

from .inference.pipeline import PIPELINE_VERSION
from .models import OpsTask, RebuildRequest, WorkerState


def flag_rebuild(reason):
    """Remember that `reason` needs a rebuild (most recent first, kept short)."""
    state = WorkerState.get()
    reasons = [reason] + [r for r in state.rebuild_reason.split("; ") if r and r != reason]
    state.rebuild_reason = "; ".join(reasons)[:300]
    state.save(update_fields=["rebuild_reason"])


def rebuild_queued():
    """A rebuild was queued: everything flagged so far, and the current counting rules, will be applied."""
    WorkerState.get()
    WorkerState.objects.filter(pk=1).update(rebuild_reason="", rules_version=PIPELINE_VERSION)


def rebuild_active():
    """True while a rebuild is queued or running (from the banner, the Queue rebuild page or Operations)."""
    return (RebuildRequest.objects.filter(processed_at__isnull=True).exists()
            or OpsTask.objects.filter(Q(status=OpsTask.QUEUED) | Q(status=OpsTask.RUNNING),
                                      kind=OpsTask.REBUILD).exists())


def rebuild_progress():
    """What the running rebuild is doing (or that it is waiting for the worker), or None if none is active."""
    if not rebuild_active():
        return None
    current = WorkerState.get().current
    if current.lower().startswith("rebuild"):
        return current.split(":", 1)[1].strip() if ":" in current else "starting"
    return "queued; the worker starts it within a minute"


def banner_reason():
    """Why a rebuild is needed, or None (nothing needed, or one is already queued or running)."""
    state = WorkerState.get()
    if state.rebuild_reason:
        reason = state.rebuild_reason
    elif state.rules_version < PIPELINE_VERSION:
        reason = "the app was updated with new counting rules"
    else:
        return None
    return None if rebuild_active() else reason


def banner():
    """("running", what it is doing) or ("needed", why) or None, for the admins' banner."""
    progress = rebuild_progress()
    if progress:
        return ("running", progress)
    reason = banner_reason()
    return ("needed", reason) if reason else None
