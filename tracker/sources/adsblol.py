"""adsb.lol globe_history adapter.

Format notes (verified against a real release, see docs/format-notes.md):
- One release per UTC day, tag `vYYYY.MM.DD-planes-readsb-{prod,staging,mlatonly}-0`,
  split into parts `<tag>.tar.aa`, `.ab`, ... (sometimes a single `<tag>.tar`).
- The repo's PREFERRED_RELEASES.txt has one line per day: comma-separated part URLs.
- Members: `./traces/<last 2 hex>/trace_full_<hex>.json`, lowercase hex, gzip bytes
  despite the .json name. Non-ICAO addresses are prefixed with `~` and are skipped.
"""
import gzip
import json
import logging
import os
import re
import tarfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Iterator

from ..geo import BBox
from .base import ScanStats, TracePoint, load_trace_file, parse_readsb_trace, raw_day_dir

log = logging.getLogger("tracker.ingest")

REPO = "adsblol/globe_history_2026"
PREFERRED_URL = "https://raw.githubusercontent.com/{repo}/main/PREFERRED_RELEASES.txt"
DOWNLOAD_URL = "https://github.com/{repo}/releases/download/{tag}/{tag}.tar{suffix}"
MEMBER_RE = re.compile(r"/traces/[0-9a-f~]{2}/trace_full_(~?[0-9a-f]{6})\.json$")
TAG_RE = re.compile(r"/download/(v(\d{4})\.(\d{2})\.(\d{2})-planes-readsb-(\w+)-\d+)/")
USER_AGENT = "helopath-phase1 (+https://adsb.lol attribution: ODbL)"


def _open(url, offset=0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset:
        req.add_header("Range", f"bytes={offset}-")
    return urllib.request.urlopen(req, timeout=120)


class ConcatStream:
    """Read several URLs (or local files) back to back as one stream, resuming
    an interrupted HTTP download with a Range request."""

    def __init__(self, parts, retries=5):
        self.parts = list(parts)
        self.retries = retries
        self.idx = -1
        self.cur = None
        self.offset = 0
        self.total = 0

    def _next_part(self):
        self.idx += 1
        self.offset = 0
        if self.idx >= len(self.parts):
            self.cur = None
            return False
        p = self.parts[self.idx]
        self.cur = open(p, "rb") if isinstance(p, Path) or not str(p).startswith("http") else _open(p)
        return True

    def read(self, n=-1):
        n = n if n and n > 0 else 1 << 20
        attempt = 0
        while True:
            if self.cur is None and not self._next_part():
                return b""
            try:
                b = self.cur.read(n)
            except (OSError, urllib.error.URLError) as e:
                attempt += 1
                if attempt > self.retries or not str(self.parts[self.idx]).startswith("http"):
                    raise
                log.warning("stream interrupted at part %d offset %d (%s); resuming", self.idx, self.offset, e)
                time.sleep(2**attempt)
                self.cur = _open(self.parts[self.idx], self.offset)
                continue
            if b:
                self.offset += len(b)
                self.total += len(b)
                return b
            self.cur.close()
            self.cur = None


def parse_preferred(text: str) -> dict[date, list[str]]:
    out = {}
    for line in text.splitlines():
        urls = [u.strip() for u in line.split(",") if u.strip()]
        if not urls:
            continue
        m = TAG_RE.search(urls[0])
        if m:
            out.setdefault(date(int(m[2]), int(m[3]), int(m[4])), urls)
    return out


class AdsbLolAdapter:
    name = "adsblol"

    def __init__(self, repo=REPO, local_parts=None):
        self.repo = repo
        self.local_parts = local_parts  # for tests and manual runs: list of local part files
        self.stats = ScanStats()

    # --- release resolution -------------------------------------------------
    def resolve_parts(self, day: date) -> tuple[str, list]:
        if self.local_parts:
            return "local:" + ",".join(os.path.basename(str(p)) for p in self.local_parts), list(self.local_parts)
        try:
            with _open(PREFERRED_URL.format(repo=self.repo)) as r:
                preferred = parse_preferred(r.read().decode())
            if day in preferred:
                urls = preferred[day]
                return TAG_RE.search(urls[0])[1], urls
        except urllib.error.URLError as e:
            log.warning("could not read PREFERRED_RELEASES.txt: %s", e)
        # Fallback: probe prod, then staging, for split parts.
        for kind in ("prod", "staging"):
            tag = f"v{day:%Y.%m.%d}-planes-readsb-{kind}-0"
            urls = []
            for suffix in (f".a{c}" for c in "abcdefghij"):
                url = DOWNLOAD_URL.format(repo=self.repo, tag=tag, suffix=suffix)
                try:
                    _open(url).close()
                except urllib.error.HTTPError:
                    break
                urls.append(url)
            if urls:
                return tag, urls
        raise FileNotFoundError(f"no adsb.lol release found for {day}")

    # --- scanning --------------------------------------------------------------
    def scan_day(self, day: date, candidates: set[str], bbox: BBox) -> Iterator[Path]:
        """Stream the day's archive; write and yield in-bbox traces of candidate hexes."""
        self.stats = st = ScanStats()
        st.release_used, parts = self.resolve_parts(day)
        out_dir = raw_day_dir(self.name, day)
        out_dir.mkdir(parents=True, exist_ok=True)
        stream = ConcatStream(parts)
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                st.members_scanned += 1
                if st.members_scanned % 5000 == 0:
                    tar.members = []  # stream mode keeps every TarInfo otherwise
                    st.bytes_streamed = stream.total
                m = MEMBER_RE.search(member.name)
                if not m or not member.isfile():
                    continue
                hexid = m[1]
                if hexid not in candidates:  # skipped members are read past, not decompressed
                    continue
                raw = tar.extractfile(member).read()
                doc = json.loads(gzip.decompress(raw))
                if not any(bbox.contains(p[1], p[2]) for p in doc.get("trace", ()) if p[1] is not None):
                    continue
                path = out_dir / f"{hexid}.json.gz"
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(path)
                st.candidates_kept += 1
                st.bytes_retained += len(raw)
                st.kept_hexes.append(hexid)
                yield path
        st.bytes_streamed = stream.total

    def parse(self, path: Path) -> Iterator[TracePoint]:
        return parse_readsb_trace(load_trace_file(path), self.name)

    def meta(self, path: Path) -> dict:
        doc = load_trace_file(path)
        return {k: doc.get(k) for k in ("icao", "r", "t", "desc", "ownOp")}
