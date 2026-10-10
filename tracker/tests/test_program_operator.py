from importlib import import_module

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, OpsTask, Operator, Program

fill = import_module("tracker.migrations.0017_fill_operator_from_program").fill


class ProgramSetsOperatorTests(TestCase):
    def setUp(self):
        self.am = Operator.objects.create(name="Air Methods")
        self.phi = Operator.objects.create(name="PHI")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.am)
        self.unknown = Program.objects.create(name="Mystery Flight")
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED)
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)

    def test_save_fills_blank_operator_only(self):
        self.a.program = self.unknown
        self.a.save(update_fields=["program"])
        self.a.refresh_from_db()
        self.assertIsNone(self.a.operator)  # program operator unknown: nothing to fill
        self.a.program = self.lf
        self.a.save(update_fields=["program"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.operator, self.am)
        self.a.operator = self.phi
        self.a.save()
        self.a.refresh_from_db()
        self.assertEqual(self.a.operator, self.phi)  # an operator already set is never replaced

    def test_use_button_sets_operator_and_queues_refresh(self):
        r = self.client.post(reverse("aircraft_apply", args=[self.a.pk]), {"field": "program", "value": self.lf.pk},
                             follow=True)
        self.a.refresh_from_db()
        self.assertEqual((self.a.program, self.a.operator), (self.lf, self.am))
        self.assertContains(r, "Program set to OSF Life Flight; operator set to Air Methods.")
        self.assertTrue(OpsTask.objects.filter(kind=OpsTask.REFRESH).exists())

    def test_admin_aircraft_edit_sets_operator(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        change = reverse("admin:tracker_aircraft_change", args=[self.a.pk])
        page = self.client.get(change)
        data = {k: v for k, v in page.context["adminform"].form.initial.items() if v not in (None, False)}
        data.update({"program": self.lf.pk, "_save": "Save"})
        r = self.client.post(change, data, follow=True)
        self.a.refresh_from_db()
        self.assertEqual(self.a.operator, self.am)
        self.assertContains(r, "Operator set to Air Methods, the operator of OSF Life Flight.")

    def test_giving_a_program_an_operator_fills_its_aircraft(self):
        self.a.program = self.unknown
        self.a.save()
        other = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", program=self.unknown, operator=self.phi)
        self.unknown.operator = self.am
        self.unknown.save()
        self.a.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.a.operator, other.operator), (self.am, self.phi))
        self.assertEqual(self.unknown.operators_filled, 1)

    def test_migration_fills_known_blanks(self):
        # Rows as they were before the save hook existed.
        Aircraft.objects.filter(pk=self.a.pk).update(program=self.lf)
        b = Aircraft.objects.create(tail_number="N2", icao_hex="a00002", operator=self.phi)
        c = Aircraft.objects.create(tail_number="N3", icao_hex="a00003")
        Aircraft.objects.filter(pk__in=[b.pk, c.pk]).update(program=self.unknown)
        mixed = Program.objects.create(name="Mixed")
        Aircraft.objects.create(tail_number="N4", icao_hex="a00004", operator=self.am)
        Aircraft.objects.create(tail_number="N5", icao_hex="a00005", operator=self.phi)
        Aircraft.objects.filter(tail_number__in=["N4", "N5"]).update(program=mixed)
        d = Aircraft.objects.create(tail_number="N6", icao_hex="a00006")
        Aircraft.objects.filter(pk=d.pk).update(program=mixed)
        fill(apps, None)
        for x in (self.a, c, d, self.unknown, mixed):
            x.refresh_from_db()
        self.assertEqual(self.a.operator, self.am)
        self.assertEqual(self.unknown.operator, self.phi)  # its aircraft all flown by PHI
        self.assertEqual(c.operator, self.phi)
        self.assertIsNone(mixed.operator)  # aircraft disagree: unknown
        self.assertIsNone(d.operator)
        self.assertEqual(OpsTask.objects.filter(kind=OpsTask.REFRESH).count(), 1)
