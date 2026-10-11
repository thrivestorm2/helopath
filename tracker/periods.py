"""Aircraft service periods: where an aircraft served (base, call sign, operator, program) between which dates.

The current period is on the Aircraft (active_from .. active_to, or open); closed ones are ServicePeriod rows.
Flights count toward the period in effect on their (local) date; flights after a retired aircraft's last day,
or before its first period, are not counted."""

from dataclasses import dataclass
from datetime import timedelta

from .models import Aircraft, ServicePeriod


@dataclass
class Span:
    start: object       # date or None (open start)
    end: object         # date or None (open end)
    base: object        # Site or None
    base_lat: float
    base_lon: float
    base_inferred: bool
    operator_id: int
    program_id: int
    call_sign: str
    current: bool = False
    period: object = None  # the ServicePeriod row for a closed period

    def contains(self, day):
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)


def current_span(a):
    base = a.base or a.inferred_base
    lat, lon = (base.lat, base.lon) if base else (a.inferred_base_lat, a.inferred_base_lon)
    return Span(a.active_from, a.active_to if a.retired else None, base, lat, lon, a.base_id is None, a.operator_id,
                a.program_id, a.call_sign, current=True)


def spans(a, past=None):
    """The aircraft's periods, oldest first; the current one last."""
    past = list(a.past_periods.select_related("base").order_by("end")) if past is None else past
    out = [Span(p.start, p.end, p.base, p.base.lat if p.base else p.base_lat, p.base.lon if p.base else p.base_lon,
                p.base_inferred, p.operator_id, p.program_id, p.call_sign, period=p) for p in past]
    return out + [current_span(a)]


def span_on(span_list, day):
    """The period in effect on a date, or None (before the first period or after retirement)."""
    for s in span_list:
        if s.contains(day):
            return s
    return None


def _archive(a, end):
    base = a.base or a.inferred_base
    return ServicePeriod.objects.create(
        aircraft=a, start=a.active_from, end=end, base=base, base_inferred=a.base_id is None and base is not None,
        base_lat=None if base else a.inferred_base_lat, base_lon=None if base else a.inferred_base_lon,
        call_sign=a.call_sign, operator_id=a.operator_id, program_id=a.program_id)


def _start_new(a, end):
    """Open a new period the day after `end`: same operator and program, no base or call sign."""
    a.active_from, a.active_to, a.retired = end + timedelta(days=1), None, False
    a.base = a.inferred_base = None
    a.inferred_base_lat = a.inferred_base_lon = None
    a.call_sign, a.call_sign_reviewed = "", False


def close_period(a, end):
    """Close the current period on `end` (its last day) and start the next one. Saves the aircraft."""
    period = _archive(a, end)
    _start_new(a, end)
    a.save()
    return period


def retire(a, end):
    """End service on `end`: the current period stays on the aircraft, closed, with no next period."""
    a.active_to, a.retired = end, True
    a.save(update_fields=["active_to", "retired"])


def unretire(a):
    """Back in service: the retired period moves to the history and a new one starts the day after it ended."""
    end = a.active_to
    if end is None:
        a.retired = False
        a.save(update_fields=["retired"])
        return None
    period = _archive(a, end)
    _start_new(a, end)
    a.save()
    return period


def spans_by_aircraft(aircraft_ids=None):
    """{aircraft pk: [Span]} for many aircraft in two queries."""
    qs = Aircraft.objects.select_related("base", "inferred_base")
    if aircraft_ids is not None:
        qs = qs.filter(pk__in=aircraft_ids)
    past = {}
    for p in ServicePeriod.objects.filter(aircraft__in=qs).select_related("base").order_by("end"):
        past.setdefault(p.aircraft_id, []).append(p)
    return {a.pk: spans(a, past.get(a.pk, [])) for a in qs}
