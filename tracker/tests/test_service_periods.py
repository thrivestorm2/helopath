from datetime import date, datetime, timedelta, timezone as dt_tz
from importlib import import_module

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker import periods
from tracker.inference import pipeline
from tracker.lookup import call_sign_suggestions
from tracker.models import Aircraft, Leg, Mission, MonthlyAggregate, Operator, Program, ServicePeriod, Site, WorkerState
from tracker.tests import PERIODS_INLINE

migration = import_module("tracker.migrations.0024_service_periods_data")


def at(d, hour=15):
    return datetime(d.year, d.month, d.day, hour, tzinfo=dt_tz.utc)


class PeriodTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", password="x")
        self.am, self.phi = Operator.objects.create(name="Air Methods"), Operator.objects.create(name="PHI")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.am)
        self.base_a = Site.objects.create(name="Base A", site_type=Site.BASE, lat=40.0, lon=-89.0)
        self.base_b = Site.objects.create(name="Base B", site_type=Site.BASE, lat=41.0, lon=-88.0)
        self.hosp = Site.objects.create(name="Hospital H", site_type=Site.HOSPITAL, lat=40.5, lon=-88.5)
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.base_a, call_sign="LIFE 1", operator=self.am, program=self.lf,
                                         active_from=date(2026, 1, 1))
        self.change = reverse("admin:tracker_aircraft_change", args=[self.a.pk])
        self.client.force_login(self.root)

    def post(self, **changes):
        page = self.client.get(self.change)
        data = {k: v for k, v in page.context["adminform"].form.initial.items() if v not in (None, False)}
        for k in ("active_from", "active_to"):
            if hasattr(data.get(k), "isoformat"):
                data[k] = data[k].isoformat()
        data.update({**changes, **PERIODS_INLINE, "_save": "Save"})
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(self.change, data, follow=True)

    def test_active_until_closes_the_period_and_starts_the_next(self):
        r = self.post(active_to="2026-09-30")
        self.assertContains(r, "Service at Base A closed on 2026-09-30; new period from 2026-10-01")
        p = ServicePeriod.objects.get(aircraft=self.a)
        self.assertEqual((p.start, p.end, p.base, p.call_sign, p.operator, p.program, p.base_inferred),
                         (date(2026, 1, 1), date(2026, 9, 30), self.base_a, "LIFE 1", self.am, self.lf, False))
        self.a.refresh_from_db()
        self.assertEqual((self.a.active_from, self.a.active_to, self.a.base, self.a.call_sign),
                         (date(2026, 10, 1), None, None, ""))
        self.assertEqual((self.a.operator, self.a.program, self.a.retired), (self.am, self.lf, False))
        self.assertIn("service period changed for N911LF", WorkerState.get().rebuild_reason)
        self.assertContains(self.client.get(reverse("aircraft_detail", args=[self.a.pk])), "Service history")

    def test_retire_and_back_in_service(self):
        self.post(retired="on")  # no date given: today
        self.a.refresh_from_db()
        self.assertEqual((self.a.retired, self.a.active_to, self.a.base), (True, timezone.localdate(), self.base_a))
        self.assertFalse(ServicePeriod.objects.exists())  # no new period
        self.assertContains(self.client.get(reverse("aircraft_detail", args=[self.a.pk])), ">retired</span>")
        Aircraft.objects.filter(pk=self.a.pk).update(active_to=date(2026, 9, 30))
        r = self.post(retired="")
        self.assertContains(r, "back in service: new period from 2026-10-01")
        self.a.refresh_from_db()
        self.assertEqual((self.a.retired, self.a.active_from, self.a.base), (False, date(2026, 10, 1), None))
        self.assertEqual(ServicePeriod.objects.get().end, date(2026, 9, 30))

    def test_until_before_from_is_refused(self):
        r = self.post(active_to="2025-12-31")
        self.assertContains(r, "&#x27;Active until&#x27; is before &#x27;Active from&#x27;.")
        self.assertFalse(ServicePeriod.objects.exists())

    def test_overlaps_are_refused(self):
        periods.close_period(self.a, date(2026, 6, 30))  # Jan..Jun closed; current from Jul 1
        with self.assertRaisesMessage(ValidationError, "Must end before the current period starts (2026-07-01)."):
            ServicePeriod(aircraft=self.a, start=date(2026, 6, 1), end=date(2026, 7, 5)).clean()
        with self.assertRaisesMessage(ValidationError, "Overlaps the period 2026-01-01 to 2026-06-30."):
            ServicePeriod(aircraft=self.a, start=date(2025, 6, 1), end=date(2026, 1, 15)).clean()
        ServicePeriod(aircraft=self.a, start=date(2025, 6, 1), end=date(2025, 12, 31)).clean()  # fine

    def trip(self, day, base):
        """Base -> hospital -> base on `day`."""
        for h, start, end, state in ((15, base, self.hosp, Leg.LANDED), (17, self.hosp, base, Leg.LANDED)):
            Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=at(day, h),
                               end_ts=at(day, h) + timedelta(minutes=30), start_lat=start.lat, start_lon=start.lon,
                               end_lat=end.lat, end_lon=end.lon, end_state=state, start_site=start, end_site=end,
                               start_on_ground=True, confidence=0.9)

    def test_flights_count_toward_the_period_they_were_in(self):
        periods.close_period(self.a, date(2026, 6, 30))
        self.a.refresh_from_db()
        self.a.base, self.a.operator, self.a.program = self.base_b, self.phi, None
        self.a.save()
        self.trip(date(2026, 3, 10), self.base_a)   # old period, at base A
        self.trip(date(2026, 8, 10), self.base_b)   # new period, at base B
        self.assertEqual(pipeline.build_missions("adsblol"), 2)
        old, new = Mission.objects.order_by("start_ts")
        self.assertEqual((old.base, old.operator, old.program), (self.base_a, self.am, self.lf))
        self.assertEqual((new.base, new.operator, new.program), (self.base_b, self.phi, None))
        pipeline.build_aggregates("adsblol")
        by_month = {(m.month, m.operator_id, m.base_id) for m in MonthlyAggregate.objects.filter(includes_unreviewed=True)}
        self.assertEqual(by_month, {(date(2026, 3, 1), self.am.pk, self.base_a.pk),
                                    (date(2026, 8, 1), self.phi.pk, self.base_b.pk)})
        # Program page counts by the program then, not the aircraft's current one.
        r = self.client.get(reverse("program_detail", args=[self.lf.pk]))
        self.assertEqual(r.context["total"], 1)

    def test_retired_aircraft_flights_after_the_end_are_not_counted(self):
        periods.retire(self.a, date(2026, 6, 30))
        self.trip(date(2026, 3, 10), self.base_a)
        self.trip(date(2026, 8, 10), self.base_a)
        self.assertEqual(pipeline.build_missions("adsblol"), 1)

    def test_new_period_base_and_call_sign_come_from_its_own_flights(self):
        self.trip(date(2026, 3, 10), self.base_a)
        Leg.objects.filter(start_ts__date=date(2026, 3, 10)).update(callsign="LIFE1")
        periods.close_period(self.a, date(2026, 6, 30))
        self.a.refresh_from_db()
        pipeline.infer_bases("adsblol")
        self.a.refresh_from_db()
        self.assertIsNone(self.a.inferred_base)  # nothing flown yet in the new period
        self.assertEqual(call_sign_suggestions(self.a, "adsblol"), [])
        self.trip(date(2026, 8, 10), self.base_b)
        Leg.objects.filter(start_ts__date=date(2026, 8, 10)).update(callsign="MERCY2")
        pipeline.infer_bases("adsblol")
        self.a.refresh_from_db()
        self.assertEqual(self.a.inferred_base, self.base_b)
        self.assertEqual(call_sign_suggestions(self.a, "adsblol"), [("MERCY2", 2)])

    def test_existing_active_until_converted_by_the_migration(self):
        other = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.CONFIRMED,
                                        operator=self.phi, base=self.base_b)
        Aircraft.objects.filter(pk=self.a.pk).update(active_to=date(2026, 9, 30))
        Mission.objects.create(aircraft=self.a, source="adsblol", start_ts=at(date(2026, 3, 1)),
                               end_ts=at(date(2026, 3, 1)), label="unknown", confidence=0.5, rule_fired="t")
        Mission.objects.create(aircraft=self.a, source="adsblol", start_ts=at(date(2026, 10, 5)),
                               end_ts=at(date(2026, 10, 5)), label="unknown", confidence=0.5, rule_fired="t")
        migration.forwards(apps, None)
        self.a.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.a.active_from, self.a.active_to, self.a.base, self.a.call_sign),
                         (date(2026, 10, 1), None, None, ""))
        self.assertEqual(ServicePeriod.objects.get().base, self.base_a)
        self.assertEqual((other.base, ServicePeriod.objects.filter(aircraft=other).count()), (self.base_b, 0))
        before, after = Mission.objects.filter(aircraft=self.a).order_by("start_ts")
        self.assertEqual((before.base, before.operator), (self.base_a, self.am))
        self.assertEqual((after.base, after.operator), (None, self.am))
