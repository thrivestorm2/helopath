"""Site seed from FAA NASR airport/heliport data (spec section 12).

Input is APT_BASE.csv from the FAA 28-day NASR subscription "CSV" download
(https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/).
Column names used: SITE_TYPE_CODE (A airport, H heliport, ...), ARPT_ID, ARPT_NAME,
CITY, STATE_CODE, LAT_DECIMAL, LONG_DECIMAL, ELEV, FACILITY_USE_CODE.
Not verified from the build environment (faa.gov is blocked there); check on first run.
"""
import csv
import io
import re

from django.conf import settings

from .geo import BBox
from .models import Site

HOSPITAL_RE = re.compile(r"\b(HOSPITAL|MEDICAL|HEALTH|MEMORIAL|CLINIC|HOSP|MED CTR|MED CENTER)\b", re.I)
TYPE_MAP = {"H": None, "A": Site.AIRPORT}  # None: decided by name


def classify_facility(site_type_code, name):
    if site_type_code == "H":
        return Site.HOSPITAL if HOSPITAL_RE.search(name) else Site.OTHER
    return TYPE_MAP.get(site_type_code)


def seed_sites(fh, bbox=None):
    """Create or update FAA-seeded sites. Rows an admin has reviewed are never overwritten."""
    bbox = bbox or BBox.from_settings()
    text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace") if not isinstance(fh, io.TextIOBase) else fh
    created = updated = skipped = 0
    for r in csv.DictReader(text):
        r = {k.strip().upper(): (v or "").strip() for k, v in r.items() if k}
        stype = classify_facility(r.get("SITE_TYPE_CODE", ""), r.get("ARPT_NAME", ""))
        try:
            lat, lon = float(r["LAT_DECIMAL"]), float(r["LONG_DECIMAL"])
        except (KeyError, ValueError):
            continue
        if stype is None or not bbox.contains(lat, lon):
            continue
        faa_id = r.get("ARPT_ID", "")
        site = Site.objects.filter(faa_id=faa_id).first() if faa_id else None
        if site and (site.reviewed or site.origin != "faa_seed"):
            skipped += 1
            continue
        values = dict(
            name=r.get("ARPT_NAME", "").title()[:200],
            site_type=stype,
            city=r.get("CITY", "").title()[:100],
            state=r.get("STATE_CODE", "")[:2],
            lat=lat,
            lon=lon,
            elevation_ft=float(r.get("ELEV") or 0),
            radius_m=settings.SITE_DEFAULT_RADIUS_M if stype != Site.AIRPORT else 1500,
            origin="faa_seed",
            reviewed=False,
            notes=f"FAA facility use: {r.get('FACILITY_USE_CODE', '?')}",
        )
        if site:
            for k, v in values.items():
                setattr(site, k, v)
            site.save()
            updated += 1
        else:
            Site.objects.create(faa_id=faa_id, **values)
            created += 1
    return created, updated, skipped
