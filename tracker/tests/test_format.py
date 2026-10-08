import gzip
import io
import json
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from django.test import TestCase, override_settings

from tracker.geo import BBox
from tracker.models import RegistryAircraft, Site
from tracker.registry import candidate_hexes, load_registry
from tracker.seed import seed_sites
from tracker.sources.adsblol import AdsbLolAdapter, parse_preferred
from tracker.sources.base import parse_readsb_trace

from . import synth

FIXTURES = Path(__file__).parent / "fixtures"
BBOX = BBox(36.5, 43.0, -92.0, -87.0)


class TraceParseTests(TestCase):
    def test_real_sample_trace(self):
        """A real adsb.lol trace (an airliner at ATL, 2026-10-06) parses into TracePoints."""
        doc = json.loads(gzip.decompress((FIXTURES / "trace_full_ad077b.json").read_bytes()))
        pts = list(parse_readsb_trace(doc, "adsblol"))
        self.assertEqual(len(pts), len(doc["trace"]))
        first = pts[0]
        self.assertEqual(first.hex, "ad077b")
        self.assertTrue(first.on_ground)
        self.assertIsNone(first.alt_baro_ft)
        self.assertEqual(first.ts_utc, datetime(2026, 10, 6, tzinfo=timezone.utc).replace(hour=19, minute=9, second=49,
                                                                                               microsecond=570000))
        airborne = [p for p in pts if not p.on_ground]
        self.assertTrue(airborne and all(isinstance(p.alt_baro_ft, float) for p in airborne if p.alt_baro_ft))

    def test_mixed_altitude_types(self):
        doc = {"icao": "ABC123", "timestamp": 0, "trace": [
            [0, 40, -89, "ground", 0, None, 0, None, None, None, None, None, None, None],
            [1, 40, -89, 1200, 90, 10, 2, -300, {"flight": "LIFE2 "}, "adsb_icao", 1300, None, None, None],
            [2, 40, -89, None, None, None, 1, None, None, None, None, None, None, None],
            [3, None, None, None, None, None, 0, None, None, None, None, None, None, None]]}
        pts = list(parse_readsb_trace(doc, "adsblol"))
        self.assertEqual(len(pts), 3)
        self.assertEqual(pts[0].hex, "abc123")
        self.assertTrue(pts[0].on_ground)
        self.assertEqual((pts[1].alt_baro_ft, pts[1].alt_geom_ft, pts[1].alt_ft), (1200.0, 1300.0, 1300.0))
        self.assertTrue(pts[1].new_leg)
        self.assertEqual(pts[1].callsign, "LIFE2")
        self.assertTrue(pts[2].stale)
        self.assertFalse(pts[2].on_ground)
        self.assertIsNone(pts[2].alt_ft)

    def test_preferred_releases(self):
        text = ("https://github.com/adsblol/globe_history_2026/releases/download/v2026.10.06-planes-readsb-staging-0/"
                "v2026.10.06-planes-readsb-staging-0.tar.aa,https://github.com/adsblol/globe_history_2026/releases/"
                "download/v2026.10.06-planes-readsb-staging-0/v2026.10.06-planes-readsb-staging-0.tar.ab\n")
        parsed = parse_preferred(text)
        self.assertEqual(list(parsed), [date(2026, 10, 6)])
        self.assertEqual(len(parsed[date(2026, 10, 6)]), 2)


class ScannerTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_scan_keeps_only_candidates_in_bbox(self):
        day = date(2026, 10, 6)
        inside, _ = synth.flight([(40.0, -89.0), (40.1, -89.2)], 50000)
        outside, _ = synth.flight([(33.6, -84.4), (33.7, -84.5)], 50000)
        docs = [synth.trace_doc("a1b2c3", day, inside), synth.trace_doc("a4d5e6", day, outside),
                synth.trace_doc("a7f8a9", day, inside)]  # last one is not a candidate
        tilde = gzip.compress(json.dumps(synth.trace_doc("~123456", day, inside)).encode())
        parts = []
        for i, chunk in enumerate(synth.split(synth.build_tar(docs, [("./traces/56/trace_full_~123456.json", tilde)]))):
            p = self.tmp / f"x.tar.a{'abc'[i]}"
            p.write_bytes(chunk)
            parts.append(p)
        with override_settings(DATA_DIR=self.tmp):
            adapter = AdsbLolAdapter(local_parts=parts)
            kept = list(adapter.scan_day(day, {"a1b2c3", "a4d5e6"}, BBOX))
        self.assertEqual([p.name for p in kept], ["a1b2c3.json.gz"])
        self.assertEqual(kept[0].parent, self.tmp / "raw/adsblol/2026/10/06")
        self.assertEqual(adapter.stats.candidates_kept, 1)
        self.assertGreater(adapter.stats.bytes_streamed, 0)
        self.assertGreater(len(list(adapter.parse(kept[0]))), 10)


class RegistryAndSeedTests(TestCase):
    def _zip(self):
        master = ("N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,STREET2,CITY,"
                  "STATE,ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,CERTIFICATION,TYPE AIRCRAFT,"
                  "TYPE ENGINE,STATUS CODE,MODE S CODE,FRACT OWNER,AIR WORTH DATE,OTHER NAMES(1),OTHER NAMES(2),"
                  "OTHER NAMES(3),OTHER NAMES(4),OTHER NAMES(5),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,"
                  "MODE S CODE HEX,\n"
                  "123AB  ,9001,1234567,,2010,3,AIR METHODS CORP  ,,,DENVER,CO,80000,,,US,,,,6,1,V,50000000,,,,,,,,,1,,,"
                  "A1B2C3    ,\n"
                  "55XY   ,1,7654321,,1999,1,JOHN SMITH,,,,IL,,,,US,,,,4,1,V,,,,,,,,,,2,,,A4D5E6    ,\n"
                  "9Z     ,2,1234567,,2001,3,WITHHELD,,,,IL,,,,US,,,,6,1,V,,,,,,,,,,3,,,          ,\n")
        ref = "CODE,MFR,MODEL,TYPE-ACFT,\n1234567,AIRBUS HELICOPTERS ,EC145 ,6,\n7654321,CESSNA ,172 ,4,\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("MASTER.txt", "﻿" + master)
            z.writestr("ACFTREF.txt", ref)
        buf.seek(0)
        return buf

    def test_registry_loads_rotorcraft_with_valid_hex(self):
        self.assertEqual(load_registry(self._zip()), 1)
        r = RegistryAircraft.objects.get()
        self.assertEqual((r.tail_number, r.mode_s_hex, r.make, r.model), ("N123AB", "a1b2c3", "AIRBUS HELICOPTERS",
                                                                          "EC145"))
        self.assertEqual(candidate_hexes(), {"a1b2c3"})

    def test_seed_sites(self):
        csv_text = ("EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,ARPT_NAME,FACILITY_USE_CODE,"
                    "LAT_DECIMAL,LONG_DECIMAL,ELEV\n"
                    "x,1,H,IL,1IL1,SPRINGFIELD,US,MEMORIAL MEDICAL CENTER,PR,39.80,-89.65,620\n"
                    "x,2,H,IL,2IL2,CHICAGO,US,NEWS TOWER,PR,41.88,-87.63,600\n"
                    "x,3,A,IL,SPI,SPRINGFIELD,US,ABRAHAM LINCOLN CAPITAL,PU,39.84,-89.68,598\n"
                    "x,4,H,GA,9GA9,ATLANTA,US,GRADY HOSPITAL,PR,33.75,-84.38,1000\n"
                    "x,5,G,IL,5IL5,X,US,GLIDERPORT,PR,40.0,-89.0,600\n")
        c, u, s = seed_sites(io.StringIO(csv_text))
        self.assertEqual((c, u, s), (3, 0, 0))
        self.assertEqual(Site.objects.get(faa_id="1IL1").site_type, Site.HOSPITAL)
        self.assertEqual(Site.objects.get(faa_id="2IL2").site_type, Site.OTHER)
        self.assertEqual(Site.objects.get(faa_id="SPI").site_type, Site.AIRPORT)
        self.assertFalse(Site.objects.filter(reviewed=True).exists())
        Site.objects.filter(faa_id="1IL1").update(reviewed=True, name="Fixed by admin")
        c, u, s = seed_sites(io.StringIO(csv_text))
        self.assertEqual((c, u, s), (0, 2, 1))
        self.assertEqual(Site.objects.get(faa_id="1IL1").name, "Fixed by admin")
