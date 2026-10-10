from datetime import date

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, AutoConfirmRule, MonthlyAggregate, OpsTask, Operator, Program


class OperatorAcquiredTests(TestCase):
    def setUp(self):
        self.buyer = Operator.objects.create(name="Air Methods")
        self.acquired = Operator.objects.create(name="Life Flight Network", notes="bought 2026")
        self.sub = Program.objects.create(name="LFN East", operator=self.acquired)
        self.a1 = Aircraft.objects.create(tail_number="N1LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                          operator=self.acquired)
        self.a2 = Aircraft.objects.create(tail_number="N2LF", icao_hex="a00002", status=Aircraft.REJECTED,
                                          operator=self.acquired, program=self.sub)
        self.other = Aircraft.objects.create(tail_number="N9AM", icao_hex="a00009", status=Aircraft.CONFIRMED,
                                             operator=self.buyer)
        self.rule = AutoConfirmRule.objects.create(registrant_contains="LIFE FLIGHT NETWORK", operator=self.acquired)
        MonthlyAggregate.objects.create(source="adsblol", operator=self.acquired, month=date(2026, 9, 1),
                                        label="scene", count=4, mean_confidence=0.9, includes_unreviewed=True)
        self.url = reverse("operator_acquired", args=[self.acquired.pk])
        self.page = reverse("operator_detail", args=[self.acquired.pk])

    def test_superuser_turns_operator_into_buyers_program(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(self.page), "Acquired by another operator")
        r = self.client.post(self.url, {"buyer": self.buyer.pk}, follow=True)
        program = Program.objects.get(name="Life Flight Network")
        self.assertRedirects(r, reverse("program_detail", args=[program.pk]))
        self.assertContains(r, "Life Flight Network is now a program of Air Methods; 2 aircraft moved.")
        self.assertEqual((program.operator, program.notes), (self.buyer, "bought 2026"))
        self.assertFalse(Operator.objects.filter(pk=self.acquired.pk).exists())
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual((self.a1.operator, self.a1.program), (self.buyer, program))  # had none: joins it
        self.assertEqual((self.a2.operator, self.a2.program), (self.buyer, self.sub))  # keeps its own program
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.operator, self.buyer)
        self.rule.refresh_from_db()
        self.assertEqual((self.rule.operator, self.rule.program), (self.buyer, program))  # kept, with the program
        self.assertTrue(self.rule.is_active)
        self.assertEqual(MonthlyAggregate.objects.get().operator, self.buyer)
        self.assertTrue(OpsTask.objects.filter(kind=OpsTask.REFRESH).exists())  # aggregates rebuilt properly
        self.other.refresh_from_db()
        self.assertEqual((self.other.operator, self.other.program), (self.buyer, None))  # untouched

    def test_reuses_existing_program_with_same_name(self):
        existing = Program.objects.create(name="life flight network")
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.client.post(self.url, {"buyer": self.buyer.pk})
        self.assertEqual(Program.objects.filter(name__iexact="life flight network").count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.operator, self.buyer)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.program, existing)

    def test_every_aircraft_in_the_programs_follows_the_buyer(self):
        stray = Aircraft.objects.create(tail_number="N7LF", icao_hex="a00007", status=Aircraft.CONFIRMED,
                                        program=self.sub)  # in the acquired operator's program, no operator
        named = Program.objects.create(name="Life Flight Network")  # an existing same-named program
        elsewhere = Operator.objects.create(name="Other Co")
        old = Aircraft.objects.create(tail_number="N8LF", icao_hex="a00008", status=Aircraft.CONFIRMED,
                                      program=named, operator=elsewhere)
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.client.post(self.url, {"buyer": self.buyer.pk})
        for a in (stray, old):
            a.refresh_from_db()
            self.assertEqual(a.operator, self.buyer, a)
        # Nothing outside those programs changed.
        self.assertEqual(list(Aircraft.objects.filter(operator=elsewhere)), [])
        self.other.refresh_from_db()
        self.assertEqual(self.other.program, None)

    def test_rule_keeps_its_own_program(self):
        self.rule.program = self.sub
        self.rule.save()
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.client.post(self.url, {"buyer": self.buyer.pk})
        self.rule.refresh_from_db()
        self.assertEqual((self.rule.operator, self.rule.program), (self.buyer, self.sub))

    def test_rule_with_program_sets_it_on_matching_aircraft(self):
        from tracker.rules import apply_auto_confirm_rules
        program = Program.objects.create(name="Life Flight Network", operator=self.buyer)
        AutoConfirmRule.objects.update(operator=self.buyer, program=program)
        new = Aircraft.objects.create(tail_number="N5LF", icao_hex="a00005", status=Aircraft.DISCOVERED,
                                      registrant_hint="LIFE FLIGHT NETWORK LLC")
        has = Aircraft.objects.create(tail_number="N6LF", icao_hex="a00006", status=Aircraft.DISCOVERED,
                                      registrant_hint="LIFE FLIGHT NETWORK LLC", program=self.sub)
        apply_auto_confirm_rules()
        new.refresh_from_db()
        has.refresh_from_db()
        self.assertEqual((new.status, new.operator, new.program), (Aircraft.CONFIRMED, self.buyer, program))
        self.assertEqual(has.program, self.sub)  # a program already set is never overwritten

    def test_only_superusers(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        self.assertNotContains(self.client.get(self.page), "Acquired by another operator")
        self.assertEqual(self.client.post(self.url, {"buyer": self.buyer.pk}).status_code, 403)
        self.assertTrue(Operator.objects.filter(pk=self.acquired.pk).exists())

    def test_cannot_acquire_itself(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.post(self.url, {"buyer": self.acquired.pk}, follow=True)
        self.assertContains(r, "Choose a different operator as the buyer.")
        self.assertTrue(Operator.objects.filter(pk=self.acquired.pk).exists())
        self.assertEqual(self.client.post(self.url, {"buyer": 999}).status_code, 404)
