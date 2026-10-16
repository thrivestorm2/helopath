"""Operator and program stay in step, and no aircraft (nor any part of its service history) is confirmed without
an operator."""

import io
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta
from importlib import import_module

from django.apps import apps
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.inference import pipeline
from tracker.inference.pipeline import local_tz
from tracker.models import (Aircraft, AutoConfirmRule, Leg, Mission, MonthlyAggregate, Operator, OpsTask,
                            PendingRebuild, Program, ServicePeriod)
from tracker.periods import confirmed_q

from . import PERIODS_INLINE, login_viewer

needs_operator = import_module("tracker.migrations.0031_confirmed_needs_operator").forwards


def _admin(client):
    admin = User.objects.create_user("boss", password="x", is_staff=True)
    admin.groups.add(Group.objects.get(name="Admin"))
    client.force_login(admin)
    return admin


class Fixtures(TestCase):
    def setUp(self):
        self.am, self.phi = Operator.objects.create(name="Air Methods"), Operator.objects.create(name="PHI")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.am)
        self.phi_prog = Program.objects.create(name="PHI Air Medical 7", operator=self.phi)
        self.open = Program.objects.create(name="Mystery Flight")  # no operator yet
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         operator=self.am, program=self.lf)

    def edit(self, aircraft, **changes):
        """Post the aircraft's admin form as it stands, with `changes`."""
        change = reverse("admin:tracker_aircraft_change", args=[aircraft.pk])
        form = self.client.get(change).context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v not in (None, False)}
        data.update({"_save": "Save", **PERIODS_INLINE, **changes})
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(change, data, follow=True)


class ProgramOperatorTests(Fixtures):
    def test_changing_a_programs_operator_moves_all_its_aircraft_but_not_their_history(self):
        b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED, program=self.lf)
        past = ServicePeriod.objects.create(aircraft=self.a, end=date(2026, 1, 31), operator=self.am, program=self.lf)
        elsewhere = Aircraft.objects.create(tail_number="N3", icao_hex="a00003", status=Aircraft.CONFIRMED,
                                            operator=self.am)
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        change = reverse("admin:tracker_program_change", args=[self.lf.pk])
        r = self.client.post(change, {"name": self.lf.name, "operator": self.phi.pk, "_save": "Save"}, follow=True)
        self.assertContains(r, "Operator set to PHI for 2 aircraft in OSF Life Flight. Past service periods are unchanged.")
        for x in (self.a, b, past, elsewhere):
            x.refresh_from_db()
        self.assertEqual((self.a.operator, b.operator, past.operator, elsewhere.operator),
                         (self.phi, self.phi, self.am, self.am))
        self.assertTrue(OpsTask.objects.filter(kind=OpsTask.REFRESH).exists())  # totals are kept per operator

    def test_clearing_a_programs_operator_leaves_its_aircraft_alone(self):
        self.lf.operator = None
        self.lf.save()
        self.a.refresh_from_db()
        self.assertEqual((self.a.operator, self.a.status), (self.am, Aircraft.CONFIRMED))


class AircraftFormTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("root", password="x"))

    def test_picking_a_program_picks_its_operator(self):
        r = self.edit(self.a, program=self.phi_prog.pk)
        self.a.refresh_from_db()
        self.assertEqual((self.a.program, self.a.operator), (self.phi_prog, self.phi))
        self.assertContains(r, "Operator set to PHI, the operator of PHI Air Medical 7.")

    def test_picking_another_operator_clears_the_old_operators_program(self):
        self.edit(self.a, operator=self.phi.pk)
        self.a.refresh_from_db()
        self.assertEqual((self.a.operator, self.a.program, self.a.status), (self.phi, None, Aircraft.CONFIRMED))

    def test_a_program_with_no_operator_keeps_the_aircrafts(self):
        self.edit(self.a, program=self.open.pk)
        self.a.refresh_from_db()
        self.assertEqual((self.a.operator, self.a.program), (self.am, self.open))

    def test_confirming_needs_an_operator(self):
        b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED)
        r = self.edit(b, status=Aircraft.CONFIRMED)
        self.assertContains(r, "Choose an operator before confirming this aircraft.")
        b.refresh_from_db()
        self.assertEqual(b.status, Aircraft.DISCOVERED)
        self.edit(b, status=Aircraft.CONFIRMED, program=self.lf.pk)  # a program with an operator is enough
        b.refresh_from_db()
        self.assertEqual((b.status, b.operator), (Aircraft.CONFIRMED, self.am))

    def test_clearing_the_operator_unconfirms(self):
        self.a.program = None
        self.a.save()
        r = self.edit(self.a, operator="")
        self.a.refresh_from_db()
        self.assertEqual((self.a.status, self.a.operator), (Aircraft.DISCOVERED, None))
        self.assertContains(r, "N911LF is unconfirmed: it has no operator. It is back in the review queue.")
        self.assertTrue(OpsTask.objects.filter(kind=OpsTask.REFRESH).exists())

    def test_program_search_narrows_to_the_chosen_operator(self):
        url = reverse("admin:autocomplete")
        params = {"app_label": "tracker", "model_name": "aircraft", "field_name": "program", "term": ""}

        def names(**extra):
            return [r["text"] for r in self.client.get(url, {**params, **extra}).json()["results"]]
        self.assertEqual(names(), ["Mystery Flight", "OSF Life Flight", "PHI Air Medical 7"])
        self.assertEqual(names(operator=self.phi.pk), ["PHI Air Medical 7", "Mystery Flight"])  # its own first

    def test_form_carries_each_programs_operator_for_its_script(self):
        r = self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk]))
        self.assertEqual(r.context["program_operators"],
                         {self.lf.pk: [self.am.pk, "Air Methods"], self.phi_prog.pk: [self.phi.pk, "PHI"]})
        self.assertContains(r, 'id="program-operators"')


class ReviewQueueTests(Fixtures):
    def setUp(self):
        super().setUp()
        _admin(self.client)
        self.b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED)

    def test_confirm_waits_for_an_operator(self):
        page = self.client.get(reverse("review_queue"))
        self.assertContains(page, 'value="confirm" disabled>Confirm</button>')
        self.assertContains(page, "Set an operator (or a program with one) to confirm.")
        r = self.client.post(reverse("review_action", args=[self.b.pk]), {"action": "confirm"}, follow=True)
        self.assertContains(r, "Choose an operator before confirming N2.")
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, Aircraft.DISCOVERED)
        self.client.post(reverse("review_action", args=[self.b.pk]), {"action": "program", "program_name": "OSF Life Flight"})
        self.client.post(reverse("review_action", args=[self.b.pk]), {"action": "confirm"})
        self.b.refresh_from_db()
        self.assertEqual((self.b.status, self.b.operator), (Aircraft.CONFIRMED, self.am))

    def test_another_operator_clears_the_program(self):
        self.client.post(reverse("review_action", args=[self.b.pk]), {"action": "program", "program_name": "OSF Life Flight"})
        r = self.client.post(reverse("review_action", args=[self.b.pk]), {"action": "operator", "operator_name": "PHI"},
                             follow=True)
        self.b.refresh_from_db()
        self.assertEqual((self.b.operator, self.b.program), (self.phi, None))
        self.assertContains(r, "operator PHI; program OSF Life Flight cleared (flown by Air Methods).")

    def test_clearing_the_operator_of_a_confirmed_aircraft_unconfirms_it(self):
        self.a.program = None
        self.a.save()
        r = self.client.post(reverse("review_action", args=[self.a.pk]), {"action": "operator", "operator_name": ""},
                             follow=True)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, Aircraft.DISCOVERED)
        self.assertContains(r, "N911LF: operator cleared; unconfirmed: it has no operator.")
        self.assertTrue(OpsTask.objects.filter(kind=OpsTask.REFRESH).exists())

    def test_a_programs_operator_cannot_be_cleared_on_its_own(self):
        r = self.client.post(reverse("review_action", args=[self.a.pk]), {"action": "operator", "operator_name": ""},
                             follow=True)
        self.a.refresh_from_db()
        self.assertEqual((self.a.operator, self.a.status), (self.am, Aircraft.CONFIRMED))
        self.assertContains(r, "N911LF: operator Air Methods.")


class RuleTests(Fixtures):
    def test_rule_with_a_program_confirms_with_the_programs_operator(self):
        AutoConfirmRule.objects.create(registrant_contains="LIFE FLIGHT", program=self.lf)
        b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED,
                                    registrant_hint="OSF LIFE FLIGHT")
        from tracker.rules import apply_auto_confirm_rules
        apply_auto_confirm_rules()
        b.refresh_from_db()
        self.assertEqual((b.status, b.operator), (Aircraft.CONFIRMED, self.am))

    def test_admin_warns_about_a_rule_that_cannot_confirm(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.post(reverse("admin:tracker_autoconfirmrule_add"),
                             {"registrant_contains": "NEWS COPTER", "is_active": "on", "_save": "Save"}, follow=True)
        self.assertContains(r, "This rule has no operator (nor a program with one), so it cannot confirm aircraft")


class PeriodWithoutOperatorTests(Fixtures):
    """A past service period with no operator is unconfirmed for its dates."""

    def setUp(self):
        super().setUp()
        ServicePeriod.objects.create(aircraft=self.a, start=date(2026, 3, 1), end=date(2026, 3, 31))
        self.a.active_from = date(2026, 4, 1)
        self.a.save()

        def fly(day, hour=12):
            t = datetime.combine(day, time(hour), tzinfo=local_tz())
            Leg.objects.create(source="adsblol", hex="a00001", aircraft=self.a, start_ts=t,
                               end_ts=t + timedelta(minutes=20), start_lat=40, start_lon=-89, end_lat=40.1,
                               end_lon=-89, end_state=Leg.LANDED, confidence=0.9)
            Mission.objects.create(aircraft=self.a, source="adsblol", start_ts=t, end_ts=t + timedelta(minutes=30),
                                   label="scene", confidence=0.8, rule_fired="t")
        fly(date(2026, 2, 28), 23)  # before the gap (late evening, local)
        fly(date(2026, 3, 1), 0)    # first minute-ish of the gap
        fly(date(2026, 3, 31), 23)  # last evening of the gap
        fly(date(2026, 4, 1), 1)    # after it

    def test_flights_in_the_gap_are_left_out_of_confirmed_only(self):
        days = sorted(m.start_ts.astimezone(local_tz()).date() for m in Mission.objects.filter(confirmed_q()))
        self.assertEqual(days, [date(2026, 2, 28), date(2026, 4, 1)])
        self.assertEqual(Leg.objects.filter(confirmed_q()).count(), 2)

    def test_monthly_totals_and_pages(self):
        pipeline.build_aggregates("adsblol")

        def total(includes_unreviewed):
            return sum(MonthlyAggregate.objects.filter(includes_unreviewed=includes_unreviewed)
                       .values_list("count", flat=True))
        self.assertEqual((total(True), total(False)), (4, 2))
        _admin(self.client)
        self.assertEqual(self.client.get(reverse("missions")).context["page"].paginator.count, 4)
        login_viewer(self.client)
        self.assertEqual(self.client.get(reverse("missions")).context["page"].paginator.count, 2)


class MigrationTests(TestCase):
    def test_confirmed_without_operator_take_the_programs_or_go_back_to_review(self):
        User.objects.create_superuser("owner", password="x")
        am = Operator.objects.create(name="Air Methods")
        lf = Program.objects.create(name="OSF Life Flight", operator=am)
        a = Aircraft.objects.create(tail_number="N1", icao_hex="a00001", status=Aircraft.DISCOVERED)
        b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", status=Aircraft.DISCOVERED)
        ok = Aircraft.objects.create(tail_number="N3", icao_hex="a00003", status=Aircraft.CONFIRMED, operator=am)
        # Rows as they were before the rule existed.
        Aircraft.objects.filter(pk=a.pk).update(status=Aircraft.CONFIRMED, program=lf)
        Aircraft.objects.filter(pk=b.pk).update(status=Aircraft.CONFIRMED)
        with redirect_stdout(io.StringIO()) as out:
            needs_operator(apps, None)
        for x in (a, b, ok):
            x.refresh_from_db()
        self.assertEqual([(x.status, x.operator) for x in (a, b, ok)],
                         [(Aircraft.CONFIRMED, am), (Aircraft.DISCOVERED, None), (Aircraft.CONFIRMED, am)])
        self.assertIn("1 took their program's operator, 1 moved back to the review queue", out.getvalue())
        self.assertEqual(LogEntry.objects.get(object_id=str(b.pk)).change_message,
                         "Unconfirmed: no operator (confirmed aircraft need one)")
        self.assertTrue(PendingRebuild.objects.filter(kind="recount").exists())
