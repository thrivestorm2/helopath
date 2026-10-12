"""Import sites (usually bases from an operator's website) from a CSV, super admins only.

Each row is matched and completed before anything is saved, and shown for approval:
- location: the row's latitude/longitude, else its address looked up on OpenStreetMap (a street location, so it
  never moves an existing site);
- FAA: a heliport within HELIPAD_MATCH_M (usually the base's own pad) gives the FAA ID, the FAA position and
  elevation, and marks the site reviewed; an airport nearby is only noted, since a base at an airport is its own
  site, not the airport;
- an existing site with the same FAA ID or name, or within EXISTING_MATCH_M, is updated instead of duplicated,
  except that a hospital or airport is never made into a base, moved or renamed: it only gets the row's note.
Nothing is ever removed or deactivated, and blank cells never erase anything.
"""

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry

from .geo import SiteIndex, haversine_m
from .models import FaaFacility, Site
from .seed import default_radius

COLUMNS = ["name", "program", "operator", "site_type", "street", "city", "state", "zip", "latitude", "longitude",
           "faa_id", "source_url", "notes"]
HELIPAD_MATCH_M = 300
AIRPORT_NOTE_M = 2000
EXISTING_MATCH_M = 500
MOVE_MIN_M = 25          # closer than this is the same place: not shown as a change
GEOCODE_MAX = 20         # OpenStreetMap allows one lookup a second; more rows need coordinates in the CSV
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
TYPES = {value for value, _ in Site.TYPE_CHOICES}


@dataclass
class ImportRow:
    n: int                       # row number in the file (2 = first data row)
    name: str
    site_type: str = Site.BASE
    lat: float = None
    lon: float = None
    located_by: str = ""         # "csv", "faa", "address" or "" (could not locate)
    faa: object = None           # FaaFacility heliport matched
    existing: object = None      # Site this row updates
    status: str = ""             # new / update / covered / same / unlocated / invalid
    outside: bool = False        # outside the study area
    changes: list = field(default_factory=list)  # [(field label, before, after)]
    values: dict = field(default_factory=dict)   # fields to set
    note: str = ""               # text added to the site's notes
    problem: str = ""

    @property
    def ticked(self):
        return self.status in ("new", "update") and not self.outside

    @property
    def selectable(self):
        return self.status in ("new", "update", "covered")


def read_csv(text):
    """[dict] keyed by lower-case column name; blank lines skipped. Raises ValueError without a name column."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames or []]
    if "name" not in reader.fieldnames:
        raise ValueError("The first row must be the column names, including name (see the example columns).")
    return [{k: (v or "").strip() for k, v in r.items() if k} for r in reader if any((v or "").strip()
                                                                                for v in r.values() if v)]


def geocode(row, opener=urllib.request.urlopen):
    """(lat, lon) for a row's address from OpenStreetMap, or None."""
    params = {"format": "jsonv2", "countrycodes": "us", "limit": 1, "street": row.get("street", ""),
              "city": row.get("city", ""), "state": row.get("state", ""), "postalcode": row.get("zip", "")}
    req = urllib.request.Request(NOMINATIM_SEARCH + "?" + urllib.parse.urlencode({k: v for k, v in params.items()
                                                                                    if v != ""}),
                                 headers={"User-Agent": "helopath site import"})
    with opener(req, timeout=10) as r:
        found = json.loads(r.read())
    return (float(found[0]["lat"]), float(found[0]["lon"])) if found else None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _inside(lat, lon):
    return (settings.BBOX_LAT_MIN <= lat <= settings.BBOX_LAT_MAX
            and settings.BBOX_LON_MIN <= lon <= settings.BBOX_LON_MAX)


def _note(r, airport):
    address = ", ".join(x for x in (r.get("street"), r.get("city"),
                                    " ".join(x for x in (r.get("state"), r.get("zip")) if x)) if x)
    parts = [f"Imported from {r['source_url']}" if r.get("source_url") else "Imported from CSV",
             *(f"{label}: {r[key]}" for key, label in (("program", "Program"), ("operator", "Operator")) if r.get(key)),
             f"Address: {address}" if address else "", f"at {airport.name} ({airport.faa_id})" if airport else "",
             r.get("notes", "")]
    return "; ".join(p for p in parts if p)


def analyse(rows, coords=None, geocoder=geocode, pause=1.0):
    """[ImportRow] for parsed CSV rows. `coords` {row number: [lat, lon] or None} holds addresses already looked
    up (so approving does not look them up again); it is filled in as addresses are looked up."""
    coords = {} if coords is None else coords
    sites = list(Site.objects.all())
    index, by_faa = SiteIndex(sites), {s.faa_id.upper(): s for s in sites if s.faa_id}
    by_name = {s.name.strip().lower(): s for s in sites}
    out, lookups = [], 0
    for i, r in enumerate(rows, start=2):
        row = ImportRow(n=i, name=r.get("name", "")[:200])
        out.append(row)
        if not row.name:
            row.status, row.problem = "invalid", "No name."
            continue
        row.site_type = r.get("site_type", "").lower() if r.get("site_type", "").lower() in TYPES else Site.BASE
        lat, lon = _float(r.get("latitude")), _float(r.get("longitude"))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            row.lat, row.lon, row.located_by = lat, lon, "csv"
        elif str(i) in coords or i in coords:
            found = coords.get(str(i), coords.get(i))
            if found:
                row.lat, row.lon, row.located_by = found[0], found[1], "address"
        elif (r.get("street") or r.get("city")) and lookups < GEOCODE_MAX:
            if lookups:
                time.sleep(pause)
            lookups += 1
            try:
                found = geocoder(r)
            except Exception:
                found = None
            coords[str(i)] = list(found) if found else None
            if found:
                row.lat, row.lon, row.located_by = found[0], found[1], "address"
        if row.lat is None:
            row.status = "unlocated"
            if not (r.get("street") or r.get("city")):
                row.problem = "No coordinates or address."
            elif str(i) in coords:
                row.problem = "Address not found on OpenStreetMap."
            else:
                row.problem = f"Only {GEOCODE_MAX} addresses are looked up per file: add its latitude/longitude."
            continue

        # FAA: the named facility, else a heliport right there; an airport nearby is only noted.
        faa = (FaaFacility.objects.filter(faa_id__iexact=r.get("faa_id")).first() if r.get("faa_id") else None)
        near = [(haversine_m(row.lat, row.lon, f.lat, f.lon), f) for f in FaaFacility.objects.filter(
            lat__range=(row.lat - 0.03, row.lat + 0.03), lon__range=(row.lon - 0.04, row.lon + 0.04))]
        near.sort(key=lambda t: t[0])
        if faa is None:
            faa = next((f for d, f in near if d <= HELIPAD_MATCH_M and f.site_type != Site.AIRPORT), None)
        airport = None if faa else next((f for d, f in near if d <= AIRPORT_NOTE_M and f.site_type == Site.AIRPORT),
                                        None)
        if faa:
            row.faa = faa
            row.lat, row.lon, row.located_by = faa.lat, faa.lon, "faa"
        row.outside = not _inside(row.lat, row.lon)
        row.note = _note(r, airport)

        values = {"name": row.name, "site_type": row.site_type, "city": r.get("city", "")[:100],
                  "state": r.get("state", "").upper()[:2], "lat": row.lat, "lon": row.lon}
        if faa:
            values.update(faa_id=faa.faa_id, elevation_ft=faa.elevation_ft, reviewed=True)
        values = {k: v for k, v in values.items() if v not in ("", None)}

        existing = (by_faa.get(faa.faa_id.upper()) if faa else None) or by_name.get(row.name.lower())
        if existing is None:
            hit = index.within(row.lat, row.lon, EXISTING_MATCH_M)
            existing = hit[0][1] if hit else None
        if existing is None:
            row.status, row.values = "new", {**values, "radius_m": default_radius(row.site_type)}
            continue
        row.existing = existing
        if existing.site_type in (Site.HOSPITAL, Site.AIRPORT):
            # A hospital or airport is never made into a base, moved or renamed: the row only adds its note.
            row.status = "covered" if row.note not in existing.notes else "same"
            row.changes = [("Notes", "", f"+ {row.note}")] if row.status == "covered" else []
            continue
        changes, update = [], {}
        for key, label in (("name", "Name"), ("site_type", "Type"), ("faa_id", "FAA ID"), ("city", "City"),
                           ("state", "State")):
            if key in values and str(values[key]) != str(getattr(existing, key)):
                before, after = getattr(existing, key), values[key]
                if key == "site_type":
                    before, after = existing.get_site_type_display(), dict(Site.TYPE_CHOICES)[after]
                changes.append((label, before, after))
                update[key] = values[key]
        # Only exact coordinates (the CSV's own, or the FAA pad) move an existing site; an address lookup never.
        if row.located_by in ("csv", "faa"):
            moved = haversine_m(existing.lat, existing.lon, row.lat, row.lon)
            if moved >= MOVE_MIN_M:
                changes.append(("Location", f"{existing.lat:.5f}, {existing.lon:.5f}",
                                f"{row.lat:.5f}, {row.lon:.5f} ({moved:,.0f} m away)"))
                update.update(lat=row.lat, lon=row.lon)
        if faa and existing.elevation_ft != faa.elevation_ft:
            update["elevation_ft"] = faa.elevation_ft
        if faa and not existing.reviewed:
            changes.append(("Reviewed", "no", "yes"))
            update["reviewed"] = True
        if row.note and row.note not in existing.notes:
            changes.append(("Notes", "", f"+ {row.note}"))
        row.changes, row.values = changes, update
        row.status = "update" if changes else "same"
    return out


def apply(rows, user):
    """Create or update the given ImportRows; returns (created, updated). Queues the rebuilds they need."""
    from .rebuild_flag import flag_site_change
    created = updated = 0
    for row in rows:
        if row.status == "new":
            site = Site.objects.create(origin="csv_import", notes=row.note, **row.values)
            flag_site_change(None, site, set())
            LogEntry.objects.log_actions(user_id=user.pk, queryset=[site], action_flag=ADDITION,
                                         change_message="CSV import", single_object=True)
            created += 1
        elif row.status in ("update", "covered"):
            site = Site.objects.get(pk=row.existing.pk)
            old = Site.objects.get(pk=site.pk)
            for key, value in row.values.items():
                setattr(site, key, value)
            if row.note and row.note not in site.notes:
                site.notes = (site.notes + "\n" if site.notes else "") + row.note
            site.save()
            changed = set(row.values)
            flag_site_change(old, site, changed)
            LogEntry.objects.log_actions(
                user_id=user.pk, queryset=[site], action_flag=CHANGE, single_object=True,
                change_message="CSV import: " + (", ".join(c[0] for c in row.changes) or "notes"))
            updated += 1
    return created, updated
