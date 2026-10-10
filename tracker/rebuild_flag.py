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


def banner_reason():
    """Why a rebuild is needed, or None (nothing needed, or one is already queued or running)."""
    state = WorkerState.get()
    if state.rebuild_reason:
        reason = state.rebuild_reason
    elif state.rules_version < PIPELINE_VERSION:
        reason = "the app was updated with new counting rules"
    else:
        return None
    pending = (RebuildRequest.objects.filter(processed_at__isnull=True).exists()
               or OpsTask.objects.filter(Q(status=OpsTask.QUEUED) | Q(status=OpsTask.RUNNING),
                                         kind=OpsTask.REBUILD).exists())
    return None if pending else reason
