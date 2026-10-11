"""Look-up links for an aircraft's tail number, and call sign / program suggestions from data we already hold.

Social sites are not searched automatically (they block it and require a login); the links open a search for a
person to read.
"""

from collections import Counter
from urllib.parse import quote, quote_plus

from django.db.models import Q

from .models import Aircraft, Leg, Program
from .social import page_links


def lookup_links(aircraft):
    """[(label, url, title, css class)] for the tail number on photo, social and registry sites. Facebook and Instagram open
    the page an admin set (on the aircraft, else its program, else its operator), else a search."""
    tail = aircraft.tail_number.upper()
    n = tail[1:] if tail.startswith("N") else tail
    q = quote_plus(f'"{tail}"')
    pages = page_links(aircraft)

    def social(field, name, search):
        if field in pages:
            url, where = pages[field]
            return (f"{name} page", url, f"The {name} page set on {where}", f"social-{field}")
        return (f"{name} search", search, f"Search {name} for {tail}", "")

    return [
        ("Google", f"https://www.google.com/search?q={q}+helicopter", "", ""),
        social("facebook", "Facebook", f"https://www.facebook.com/search/top?q={quote_plus(tail)}"),
        social("instagram", "Instagram", f"https://www.instagram.com/explore/tags/{quote(tail.lower())}/"),
        ("JetPhotos", f"https://www.jetphotos.com/registration/{quote(tail)}", "", ""),
        ("FlightAware", f"https://www.flightaware.com/resources/registration/{quote(tail)}", "", ""),
        ("FAA registry", f"https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt={quote(n)}", "",
         ""),
    ]


def call_sign_suggestions(aircraft, source, limit=3):
    """[(flight ID, legs flown with it)] from the aircraft's ADS-B flight IDs, excluding its own tail number
    (many aircraft broadcast the registration, which says nothing new)."""
    seen = Counter()
    legs = Leg.objects.filter(source=source, aircraft=aircraft).exclude(callsign="")
    if aircraft.active_from:  # current service period only: an earlier base's call sign is not this one
        legs = legs.filter(start_ts__date__gte=aircraft.active_from)
    for ids in legs.values_list("callsign", flat=True):
        seen.update(c.strip().upper() for c in ids.split(",") if c.strip())
    tail = aircraft.tail_number.upper()
    return [(c, n) for c, n in seen.most_common() if c not in (tail, tail.lstrip("N"))][:limit]


def program_suggestions(aircraft, limit=4):
    """[(program, reason)]: programs of other aircraft at the same base, then programs its operator flies."""
    found = {}
    base = aircraft.base or aircraft.inferred_base
    if base:
        others = (Aircraft.objects.filter(Q(base=base) | Q(base__isnull=True, inferred_base=base),
                                          status__in=Aircraft.VISIBLE, program__isnull=False)
                  .exclude(pk=aircraft.pk).values_list("program", flat=True))
        for pk, n in Counter(others).most_common():
            found[pk] = f"{n} other aircraft at {base.name}"
    if aircraft.operator_id:
        for pk in Program.objects.filter(operator=aircraft.operator_id).values_list("pk", flat=True):
            found.setdefault(pk, f"flown by {aircraft.operator}")
    found.pop(aircraft.program_id, None)
    programs = Program.objects.in_bulk(list(found)[:limit])
    return [(programs[pk], why) for pk, why in found.items() if pk in programs]
