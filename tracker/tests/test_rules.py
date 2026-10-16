import hashlib
import io

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, AutoConfirmRule, OpsTask, Operator
from tracker.rules import apply_auto_confirm_rules


def make(tail, registrant, status=Aircraft.DISCOVERED, **kw):
    return Aircraft.objects.create(tail_number=tail, icao_hex="a" + hashlib.md5(tail.encode()).hexdigest()[:5],
                                   registrant_hint=registrant, status=status, discovered_at=timezone.now(), **kw)


class AutoConfirmRuleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner", password="x")

    def test_air_methods_rule_is_preloaded(self):
        rule = AutoConfirmRule.objects.get(registrant_contains="AIR METHODS LLC")
        self.assertTrue(rule.is_active)

    def test_confirms_only_waiting_matches_and_logs(self):
        hit = make("N1AM", "AIR METHODS LLC")
        lower = make("N2AM", "air methods llc")
        rejected = make("N3AM", "AIR METHODS LLC", status=Aircraft.REJECTED)
        other = make("N4XX", "NEWS COPTER INC")
        self.assertEqual(apply_auto_confirm_rules(), 0)  # the preloaded rule has no operator: nothing confirmed
        AutoConfirmRule.objects.filter(registrant_contains="AIR METHODS LLC").update(
            operator=Operator.objects.create(name="Air Methods"))
        self.assertEqual(apply_auto_confirm_rules(), 2)
        for a, status in [(hit, "confirmed"), (lower, "confirmed"), (rejected, "rejected"), (other, "discovered")]:
            a.refresh_from_db()
            self.assertEqual(a.status, status, a.tail_number)
        entry = LogEntry.objects.get(object_id=str(hit.pk))
        self.assertEqual(entry.user, self.owner)
        self.assertIn("Auto-confirmed, operator set to Air Methods by rule", entry.change_message)
        self.assertEqual(apply_auto_confirm_rules(), 0)  # idempotent

    def test_rule_sets_operator_only_when_unset(self):
        op = Operator.objects.create(name="Air Methods")
        mine = Operator.objects.create(name="Mercy Flight")
        AutoConfirmRule.objects.filter(registrant_contains="AIR METHODS LLC").update(operator=op)
        unset = make("N5AM", "AIR METHODS LLC")
        preset = make("N6AM", "AIR METHODS LLC", operator=mine)
        apply_auto_confirm_rules()
        unset.refresh_from_db()
        preset.refresh_from_db()
        self.assertEqual((unset.operator, preset.operator), (op, mine))

    def test_switched_off_rule_does_nothing(self):
        AutoConfirmRule.objects.update(is_active=False)
        a = make("N7AM", "AIR METHODS LLC")
        self.assertEqual(apply_auto_confirm_rules(), 0)
        a.refresh_from_db()
        self.assertEqual(a.status, Aircraft.DISCOVERED)

    def test_startup_command_queues_refresh_only_when_something_changed(self):
        call_command("apply_rules", stdout=io.StringIO())
        self.assertFalse(OpsTask.objects.exists())
        make("N8AM", "AIR METHODS LLC")
        AutoConfirmRule.objects.update(operator=Operator.objects.create(name="Air Methods"))
        call_command("apply_rules", stdout=io.StringIO())
        self.assertEqual(OpsTask.objects.get().kind, OpsTask.REFRESH)


class RulesScreenTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))

    def test_add_rule_confirms_waiting_aircraft_now(self):
        a = make("N9PH", "PHI HEALTH LLC")
        op = Operator.objects.create(name="PHI Air Medical")
        self.client.force_login(self.admin)
        r = self.client.post(reverse("review_rules"), {"action": "add", "registrant_contains": " PHI  HEALTH ",
                                                       "operator": op.pk}, follow=True)
        self.assertContains(r, "1 aircraft updated now")
        a.refresh_from_db()
        self.assertEqual((a.status, a.operator), (Aircraft.CONFIRMED, op))
        rule = AutoConfirmRule.objects.get(registrant_contains="PHI HEALTH")
        self.assertEqual((rule.created_by, rule.operator), (self.admin, op))
        self.assertEqual(OpsTask.objects.filter(kind=OpsTask.REFRESH).count(), 1)
        self.assertContains(r, "Registrant contains <strong>PHI HEALTH</strong>")

    def test_rule_requires_an_existing_operator(self):
        make("N10P", "PHI HEALTH LLC")
        self.client.force_login(self.admin)
        for operator in ["", "999999"]:
            r = self.client.post(reverse("review_rules"), {"action": "add", "registrant_contains": "PHI HEALTH",
                                                           "operator": operator, "operator_name": "Brand New"},
                                 follow=True)
            self.assertContains(r, "Choose an existing operator")
        self.assertFalse(AutoConfirmRule.objects.filter(registrant_contains="PHI HEALTH").exists())
        self.assertFalse(Operator.objects.exists())  # never created from a rule form

    def test_card_link_adds_rule_for_that_registrant_and_returns(self):
        op = Operator.objects.create(name="Air Evac Lifeteam")
        card = make("N11E", "AIR EVAC EMS INC")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("review_queue"))
        self.assertContains(page, 'name="registrant_contains" value="AIR EVAC EMS INC"')
        self.assertContains(page, "Auto-confirm this registrant")
        r = self.client.post(reverse("review_rules"), {"action": "add", "registrant_contains": "AIR EVAC EMS INC",
                                                       "operator": op.pk, "next": "/review/?page=2"})
        self.assertEqual(r["Location"], "/review/?page=2")
        card.refresh_from_db()
        self.assertEqual((card.status, card.operator), (Aircraft.CONFIRMED, op))

    def test_card_link_hidden_until_an_operator_exists(self):
        make("N12E", "AIR EVAC EMS INC")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("review_queue"))
        self.assertNotContains(page, "<button name=\"action\" value=\"add\">Auto-confirm this registrant")
        self.assertContains(page, "first create an operator")

    def test_setting_operator_on_preloaded_rule_fills_its_confirmed_aircraft(self):
        a = make("N13AM", "AIR METHODS LLC")
        apply_auto_confirm_rules()  # preloaded rule has no operator yet
        a.refresh_from_db()
        self.assertEqual((a.status, a.operator), (Aircraft.DISCOVERED, None))  # never confirmed without one
        op = Operator.objects.create(name="Air Methods")
        rule = AutoConfirmRule.objects.get(registrant_contains="AIR METHODS LLC")
        self.client.force_login(self.admin)
        self.client.post(reverse("review_rules"), {"action": "set_operator", "rule": rule.pk, "operator": op.pk})
        a.refresh_from_db()
        self.assertEqual((a.status, a.operator), (Aircraft.CONFIRMED, op))

    def test_most_visited_hospitals_one_per_line(self):
        make("N14H", "X", top_hospitals_hint="OSF St Francis (5); Carle (3); Memorial (1)")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("review_queue"))
        self.assertContains(page, '<br><span style="padding-left:1em">OSF St Francis (5)</span>'
                                  '<br><span style="padding-left:1em">Carle (3)</span>', html=False)

    def test_switch_off_and_validation(self):
        self.client.force_login(self.admin)
        rule = AutoConfirmRule.objects.get(registrant_contains="AIR METHODS LLC")
        self.client.post(reverse("review_rules"), {"action": "off", "rule": rule.pk})
        rule.refresh_from_db()
        self.assertFalse(rule.is_active)
        op = Operator.objects.create(name="Air Methods")
        r = self.client.post(reverse("review_rules"), {"action": "add", "registrant_contains": "AI", "operator": op.pk},
                             follow=True)
        self.assertContains(r, "at least 4 characters")

    def test_viewers_cannot_manage_rules(self):
        self.client.force_login(self.viewer)
        r = self.client.post(reverse("review_rules"), {"action": "add", "registrant_contains": "ANYTHING"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(AutoConfirmRule.objects.filter(registrant_contains="ANYTHING").exists())
