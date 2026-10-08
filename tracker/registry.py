"""FAA releasable aircraft registry loader (spec 5.2).

ReleasableAircraft.zip holds MASTER.txt (one row per registered aircraft) and
ACFTREF.txt (model code -> make/model). Both are comma delimited with a header row;
header names carry padding and sometimes a UTF-8 BOM, so keys are normalized.
"""
import csv
import io
import re
import urllib.request
import zipfile

from django.db import transaction
from django.utils import timezone

from .models import Aircraft, RegistryAircraft

REGISTRY_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
ROTORCRAFT = "6"
HEX_RE = re.compile(r"^[0-9a-f]{6}$")


def _rows(fh):
    reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))
    header = [h.strip().upper() for h in next(reader)]
    for row in reader:
        yield {k: (row[i].strip() if i < len(row) else "") for i, k in enumerate(header)}


def _find(names, wanted):
    for n in names:
        if n.upper().rsplit("/", 1)[-1] == wanted:
            return n
    raise KeyError(f"{wanted} not found in registry archive")


def load_registry(zip_source, rotorcraft_only=True):
    """Load (replace) RegistryAircraft from a path or file object. Returns rows loaded."""
    now = timezone.now()
    with zipfile.ZipFile(zip_source) as zf:
        names = zf.namelist()
        models = {}
        with zf.open(_find(names, "ACFTREF.TXT")) as fh:
            for r in _rows(fh):
                models[r.get("CODE", "")] = (r.get("MFR", ""), r.get("MODEL", ""))
        rows = []
        with zf.open(_find(names, "MASTER.TXT")) as fh:
            for r in _rows(fh):
                type_ac = r.get("TYPE AIRCRAFT", "")
                if rotorcraft_only and type_ac != ROTORCRAFT:
                    continue
                hexid = r.get("MODE S CODE HEX", "").lower()
                if not HEX_RE.match(hexid):
                    continue
                code = r.get("MFR MDL CODE", "")
                make, model = models.get(code, ("", ""))
                rows.append(
                    RegistryAircraft(
                        n_number=r.get("N-NUMBER", "")[:6],
                        mode_s_hex=hexid,
                        type_aircraft=type_ac,
                        model_code=code[:7],
                        make=make[:40],
                        model=model[:40],
                        registrant_name=r.get("NAME", "")[:60],
                        status_code=r.get("STATUS CODE", "")[:2],
                        loaded_at=now,
                    )
                )
    with transaction.atomic():
        RegistryAircraft.objects.all().delete()
        RegistryAircraft.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def download_registry(dest):
    req = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": "Mozilla/5.0 helopath"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def candidate_hexes() -> set[str]:
    """The candidate pool: registered rotorcraft, plus any manually added aircraft."""
    pool = set(RegistryAircraft.objects.filter(type_aircraft=ROTORCRAFT).values_list("mode_s_hex", flat=True))
    pool |= set(Aircraft.objects.exclude(status=Aircraft.REJECTED).values_list("icao_hex", flat=True))
    return {h for h in pool if HEX_RE.match(h)}
