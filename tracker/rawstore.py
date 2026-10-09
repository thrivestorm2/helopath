"""Retained raw traces, stored as gzip blobs in the RawTrace table (guardrail 3: rebuild source)."""
from datetime import date

from .models import RawTrace


def put(source: str, day: date, hexid: str, data: bytes):
    RawTrace.objects.update_or_create(source=source, day=day, hex=hexid, defaults={"data": data, "size": len(data)})


def hexes(source: str) -> set[str]:
    return set(RawTrace.objects.filter(source=source).values_list("hex", flat=True).distinct())


def traces_for_hex(source: str, hexid: str) -> list[bytes]:
    """All retained traces for one aircraft, oldest day first."""
    return [bytes(d) for d in RawTrace.objects.filter(source=source, hex=hexid).order_by("day").values_list(
        "data", flat=True)]


def latest_for_hex(source: str, hexid: str):
    row = RawTrace.objects.filter(source=source, hex=hexid).order_by("-day").values_list("data", flat=True).first()
    return bytes(row) if row is not None else None


def purge_before(source: str, cutoff: date, keep_hexes: set[str]) -> int:
    """Delete traces older than cutoff except for keep_hexes. Returns rows deleted."""
    n, _ = RawTrace.objects.filter(source=source, day__lt=cutoff).exclude(hex__in=keep_hexes).delete()
    return n


def stats(source: str) -> dict:
    from django.db.models import Count, Sum

    agg = RawTrace.objects.filter(source=source).aggregate(rows=Count("id"), bytes=Sum("size"))
    agg["days"] = RawTrace.objects.filter(source=source).values("day").distinct().count()
    return agg
