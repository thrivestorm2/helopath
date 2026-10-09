import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tracker.inference import pipeline
from tracker.ingest import ingest_day
from tracker.models import (Aircraft, LandingCluster, Leg, Mission, MonthlyAggregate, Operator, RawTrace,
                            RegistryAircraft, Site)
from tracker.sources.adsblol import AdsbLolAdapter

from . import synth

BASE = (40.00, -89.00)
HOSP_A = (40.10, -89.20)
HOSP_B = (40.30, -88.90)
AIRPORT = (39.90, -88.70)
SCENE = (40.20, -89.10)
UNKNOWN_BASE = (40.60, -89.60)  # where the non-medical helicopter lives; not a site

MED, OTHER = "a1b2c3", "a4d5e6"
T0 = 14 * 3600  # 14:00 UTC, 09:00 Chicago


class PipelineTests(TestCase):
    """End to end on synthetic traces: ingest -> legs -> discovery -> missions -> aggregates."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)
        mk = lambda name, t, ll: Site.objects.create(name=name, site_type=t, lat=ll[0], lon=ll[1], elevation_ft=600)
        self.base = mk("Base One", Site.BASE, BASE)
        self.hosp_a = mk("Hospital A", Site.HOSPITAL, HOSP_A)
        self.hosp_b = mk("Hospital B", Site.HOSPITAL, HOSP_B)
        self.airport = mk("Airport C", Site.AIRPORT, AIRPORT)
        Site.objects.filter(pk=self.airport.pk).update(radius_m=1500)
        now = timezone.now()
        RegistryAircraft.objects.create(n_number="123AB", mode_s_hex=MED, type_aircraft="6", make="AIRBUS",
                                        model="EC145", registrant_name="LIFE FLIGHT LEASING", loaded_at=now)
        RegistryAircraft.objects.create(n_number="55XY", mode_s_hex=OTHER, type_aircraft="6", make="BELL",
                                        model="407", registrant_name="NEWS CO", loaded_at=now)
        today = timezone.now().date()
        self.days = [today - timedelta(days=d) for d in (5, 4, 3)]

    def _day_points(self, i):
        if i == 0:
            med, t = synth.flight([BASE, HOSP_A, HOSP_B, BASE], T0)              # interfacility
        elif i == 1:
            med, t = synth.flight([BASE, SCENE, HOSP_A, BASE], T0)              # scene
        else:
            med, t = synth.flight([BASE, HOSP_B, BASE], T0)                     # single hospital
            more, _ = synth.flight([BASE, AIRPORT, BASE], t + 3600)             # reposition
            med += more
        # Non-medical helicopter: one hospital visit a day would be needed 3 times; give it airport hops only.
        other, _ = synth.flight([UNKNOWN_BASE, AIRPORT, UNKNOWN_BASE], T0)
        return med, other

    def _ingest(self, i):
        day = self.days[i]
        med, other = self._day_points(i)
        tar = synth.build_tar([synth.trace_doc(MED, day, med), synth.trace_doc(OTHER, day, other, reg="N55XY")])
        part = self.tmp / f"{day}.tar"
        part.write_bytes(tar)
        return ingest_day(day, adapter=AdsbLolAdapter(local_parts=[part]))

    def _ingest_all(self):
        for i in range(3):
            self._ingest(i)

    def test_end_to_end(self):
        self._ingest(0)
        self.assertFalse(Aircraft.objects.exists(), "one day of visits must not discover")
        self._ingest(1)
        self._ingest(2)
        # Legs exist for both candidates, but only the medical one is discovered.
        self.assertTrue(Leg.objects.filter(hex=OTHER).exists())
        a = Aircraft.objects.get()
        self.assertEqual((a.icao_hex, a.tail_number, a.status, a.origin), (MED, "N123AB", "discovered", "auto"))
        self.assertEqual(a.qualifying_visits, 4)
        self.assertEqual(a.distinct_hospitals, 2)
        self.assertEqual(a.make_model, "AIRBUS EC145")
        self.assertEqual(a.registrant_hint, "LIFE FLIGHT LEASING")
        self.assertEqual(a.callsign_hint, "LIFE1")
        self.assertEqual(a.inferred_base, self.base)
        self.assertIsNone(a.base)

        legs = Leg.objects.filter(hex=MED)
        self.assertEqual(legs.count(), 10)
        self.assertTrue(all(leg.end_state == Leg.LANDED for leg in legs))
        self.assertEqual(legs.filter(end_site__isnull=True).count(), 1)  # the scene landing
        labels = sorted(Mission.objects.values_list("label", flat=True))
        self.assertEqual(labels, ["interfacility", "reposition", "scene", "single_hospital_unresolved"])
        inter = Mission.objects.get(label="interfacility")
        self.assertEqual((inter.origin_site, inter.destination_site), (self.hosp_a, self.hosp_b))
        self.assertEqual(inter.rule_fired, "base>H>H>base")
        self.assertEqual(Mission.objects.get(label="scene").destination_site, self.hosp_a)

        aggs = MonthlyAggregate.objects.filter(includes_unreviewed=True)
        self.assertEqual(sum(aggs.values_list("count", flat=True)), 4)
        self.assertFalse(MonthlyAggregate.objects.filter(includes_unreviewed=False).exists())
        self.assertEqual(LandingCluster.objects.get().landing_count, 1)  # scene location

    def test_rebuild_is_reproducible(self):
        self._ingest_all()
        before = list(Leg.objects.order_by("hex", "start_ts").values_list("hex", "start_ts", "end_state", "end_site"))
        missions = Mission.objects.count()
        pipeline.rebuild("adsblol")
        after = list(Leg.objects.order_by("hex", "start_ts").values_list("hex", "start_ts", "end_state", "end_site"))
        self.assertEqual(before, after)
        self.assertEqual(Mission.objects.count(), missions)

    def test_admin_decisions_survive_rebuild_and_reject_sticks(self):
        self._ingest_all()
        a = Aircraft.objects.get()
        op = Operator.objects.create(name="Test Air Medical")
        a.operator, a.base, a.notes = op, self.base, "checked"
        a.save()
        pipeline.rebuild("adsblol")
        a.refresh_from_db()
        self.assertEqual((a.operator, a.base, a.notes, a.status), (op, self.base, "checked", "discovered"))

        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        self.client.post(reverse("review_action", args=[a.pk]), {"action": "reject"})
        a.refresh_from_db()
        self.assertEqual(a.status, Aircraft.REJECTED)
        self.assertFalse(MonthlyAggregate.objects.exists())
        self.assertFalse(Mission.objects.exists())
        pipeline.rebuild("adsblol")
        a.refresh_from_db()
        self.assertEqual(a.status, Aircraft.REJECTED)
        self.assertEqual(Aircraft.objects.count(), 1)
        self.assertFalse(MonthlyAggregate.objects.exists())

    def test_confirmed_only_toggle(self):
        self._ingest_all()
        Aircraft.objects.update(status=Aircraft.CONFIRMED)
        pipeline.build_aggregates("adsblol")
        self.assertEqual(sum(MonthlyAggregate.objects.filter(includes_unreviewed=False).values_list("count", flat=True)), 4)

    def test_site_deactivation_undiscovers_untouched_aircraft(self):
        self._ingest_all()
        Site.objects.filter(site_type=Site.HOSPITAL).update(is_active=False)
        pipeline.rebuild("adsblol")
        self.assertFalse(Aircraft.objects.exists())
        self.assertTrue(Leg.objects.filter(hex=MED).exists())

    def test_purge_removes_only_undiscovered_traces(self):
        self._ingest_all()
        removed = pipeline.purge_undiscovered("adsblol", today=self.days[-1] + timedelta(days=200))
        self.assertEqual(removed, 3)
        self.assertFalse(Leg.objects.filter(hex=OTHER).exists())
        self.assertEqual(RawTrace.objects.filter(hex=MED).count(), 3)
        self.assertFalse(RawTrace.objects.filter(hex=OTHER).exists())
        # Within the retention window nothing is purged.
        self.assertEqual(pipeline.purge_undiscovered("adsblol", today=self.days[-1] + timedelta(days=30)), 0)

    def test_inferred_landing_and_signal_lost(self):
        from tracker.inference.legs import infer_legs
        from tracker.geo import SiteIndex
        from tracker.sources.base import parse_readsb_trace

        day = self.days[0]
        pts, _ = synth.flight([BASE, HOSP_A], T0, land=False)
        # Cut the descent short ~1.5 km before the pad: coverage lost while low and descending.
        pts = [p for p in pts if not (p[1] > HOSP_A[0] - 0.008 and p[3] != "ground")]
        lost, _ = synth.flight([BASE, SCENE], T0 + 7200, land=False)
        lost = [p for p in lost if p[0] < T0 + 7200 + 400]  # vanish at cruise, far from anything
        doc = synth.trace_doc(MED, day, pts + lost)
        legs = infer_legs(list(parse_readsb_trace(doc, "adsblol")), SiteIndex(Site.objects.all()))
        self.assertEqual([leg.end_state for leg in legs], [Leg.INFERRED, Leg.LOST])
        self.assertEqual(legs[0].end_site, self.hosp_a)
        self.assertTrue(0.0 < legs[0].confidence < 0.9)
        self.assertEqual(legs[0].start_site, self.base)
        self.assertIsNone(legs[1].end_site)

    def _lost_near(self, gs, alt_agl, offset_deg=0.003, vrate=0, last_gs_missing=False):
        """A leg that cruises toward Hospital A and is lost ~offset_deg south of it, never reporting ground."""
        from tracker.geo import SiteIndex
        from tracker.inference.legs import infer_legs
        from tracker.sources.base import parse_readsb_trace

        lat_end = HOSP_A[0] - offset_deg
        pts = []
        for i in range(30):
            f = i / 29
            t = T0 + i * 10
            near_end = i >= 25
            alt = 600 + (alt_agl if near_end else 2000)
            speed = gs if near_end else 120
            if last_gs_missing and i == 29:
                speed = None
            pts.append([t, BASE[0] + (lat_end - BASE[0]) * f, BASE[1] + (HOSP_A[1] - BASE[1]) * f, alt, speed,
                        0.0, 0, vrate, None, "adsb_icao", None, None, None, None])
        doc = synth.trace_doc(MED, self.days[0], pts)
        return infer_legs(list(parse_readsb_trace(doc, "adsblol")), SiteIndex(Site.objects.all()))[0]

    def test_low_and_slow_at_signal_loss_lands_on_nearby_rooftop_pad(self):
        from tracker.inference.legs import is_qualifying_visit

        # 330 m short of the pad, 300 ft above site elevation (a rooftop), 25 kt, level: no ground report.
        leg = self._lost_near(gs=25, alt_agl=300)
        self.assertEqual((leg.end_state, leg.end_site), (Leg.INFERRED, self.hosp_a))
        self.assertGreaterEqual(leg.confidence, 0.6)

        class L:  # is_qualifying_visit reads these attributes
            end_site, end_state, confidence = leg.end_site, leg.end_state, leg.confidence
        self.assertTrue(is_qualifying_visit(L))

        # Speed missing on the very last point: the speed reported seconds earlier is used.
        self.assertEqual(self._lost_near(gs=25, alt_agl=300, last_gs_missing=True).end_site, self.hosp_a)

    def test_fast_or_high_or_far_signal_loss_is_not_a_landing(self):
        self.assertEqual(self._lost_near(gs=100, alt_agl=300).end_state, Leg.LOST)   # low pass, too fast
        self.assertEqual(self._lost_near(gs=25, alt_agl=800).end_state, Leg.LOST)    # too high, not descending
        self.assertEqual(self._lost_near(gs=25, alt_agl=300, offset_deg=0.02).end_state, Leg.LOST)  # 2.2 km out

    def test_pages_render_with_data(self):
        self._ingest_all()
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        a = Aircraft.objects.get()
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "90%")  # 9 of 10 medical legs end at a site
        for url in [reverse("aircraft_list"), reverse("aircraft_detail", args=[a.pk]), reverse("missions"),
                    reverse("flows"), reverse("quality"), reverse("ingest_log"),
                    reverse("site_detail", args=[self.hosp_a.pk]), reverse("site_list") + "?view=map"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("aircraft_list")), "N123AB")
        self.assertContains(self.client.get(reverse("flows_csv")), "interfacility")
        self.assertNotContains(self.client.get(reverse("aircraft_list")), "N55XY")
