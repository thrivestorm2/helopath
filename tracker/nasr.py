"""Download the FAA NASR airport/heliport CSV (APT_BASE.csv) for the site seed.

NASR is published on the 28-day AIRAC cycle. The CSV bundle is expected at
  https://nfdc.faa.gov/webContent/28DaySub/extra/<DD_Mon_YYYY>_APT_CSV.zip
UNVERIFIED: faa.gov was unreachable from the build environment. If this fails, upload
APT_BASE.csv (or the zip) on the Operations page instead.
"""
import io
import urllib.request
import zipfile
from datetime import date, timedelta

AIRAC_ANCHOR = date(2025, 1, 23)  # AIRAC cycle 2501 effective date
CYCLE = timedelta(days=28)
URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/{d:%d}_{d:%b}_{d:%Y}_APT_CSV.zip"


def cycle_dates(today: date, count=2):
    """Effective date of the current cycle, then earlier ones (newest first)."""
    current = AIRAC_ANCHOR + CYCLE * ((today - AIRAC_ANCHOR) // CYCLE)
    return [current - CYCLE * i for i in range(count)]


def extract_apt_base(blob: bytes) -> bytes:
    """APT_BASE.csv bytes from a NASR CSV zip, or the blob itself if it is already the CSV."""
    if blob[:2] != b"PK":
        return blob
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if name.upper().rsplit("/", 1)[-1] == "APT_BASE.CSV":
                return z.read(name)
    raise KeyError("APT_BASE.csv not found in the uploaded zip")


def download_apt_base(today: date, log=print) -> bytes:
    errors = []
    for d in cycle_dates(today):
        url = URL.format(d=d)
        log(f"trying {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 helopath"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return extract_apt_base(r.read())
        except Exception as e:  # try the previous cycle before giving up
            errors.append(f"{url}: {e}")
    raise RuntimeError("could not download NASR APT data; upload APT_BASE.csv on the Operations page instead.\n"
                       + "\n".join(errors))
