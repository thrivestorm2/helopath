from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, AutoConfirmRule, Operator, Program, RegistryAircraft
from tracker.nnumber import FIRST, LAST, hex_to_tail, tail_to_hex


class NNumberTests(SimpleTestCase):
    def test_known_ends_and_order(self):
        self.assertEqual([tail_to_hex(t) for t in ("N1", "N1A", "N1AA", "N99999")],
                         ["a00001", "a00002", "a00003", "adf7c7"])
        self.assertEqual((hex_to_tail("a00001"), hex_to_tail("ADF7C7")), ("N1", "N99999"))
        self.assertEqual(tail_to_hex("123ab"), tail_to_hex("N123AB"))  # N and case optional

    def test_every_code_converts_both_ways(self):
        tails = set()
        for value in range(FIRST, LAST + 1):
            tail = hex_to_tail(f"{value:06x}")
            self.assertEqual(tail_to_hex(tail), f"{value:06x}")
            tails.add(tail)
        self.assertEqual(len(tails), LAST - FIRST + 1)  # one tail per code

    def test_invalid(self):
        for tail in ("N0", "N1I", "N1O", "N123456", "N1ABC", "N12A3", "C-GABC", "", None):
            self.assertIsNone(tail_to_hex(tail), tail)
        for hexid in ("a00000", "adf7c8", "c0ffee", "xyz", "a0001"):
            self.assertIsNone(hex_to_tail(hexid), hexid)


class LookupTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        self.hexid = tail_to_hex("N123AB")
        RegistryAircraft.objects.create(n_number="123AB", mode_s_hex=self.hexid, type_aircraft="6", make="AIRBUS",
                                        model="EC145", registrant_name="LIFE FLIGHT LEASING LLC",
                                        loaded_at=timezone.now())
        self.op = Operator.objects.create(name="Air Methods")
        self.prog = Program.objects.create(name="OSF Life Flight", operator=self.op)
        AutoConfirmRule.objects.create(registrant_contains="life flight", operator=self.op, program=self.prog)

    def get(self, **params):
        return self.client.get(reverse("aircraft_lookup"), params).json()

    def test_tail_fills_hex_registry_and_rule(self):
        d = self.get(tail="n123ab")
        self.assertEqual((d["tail"], d["hex"]), ("N123AB", self.hexid))
        self.assertEqual(d["registry"], {"make_model": "AIRBUS EC145", "owner": "LIFE FLIGHT LEASING LLC"})
        self.assertEqual((d["operator"]["id"], d["program"]["id"]), (self.op.pk, self.prog.pk))

    def test_plane_not_in_registry_still_gets_its_hex(self):
        d = self.get(tail="N77MP")
        self.assertEqual(d, {"tail": "N77MP", "hex": tail_to_hex("N77MP")})
        self.assertEqual(self.get(hex=tail_to_hex("N77MP"))["tail"], "N77MP")

    def test_already_tracked_and_non_us(self):
        a = Aircraft.objects.create(tail_number="N123AB", icao_hex=self.hexid, status=Aircraft.REJECTED)
        d = self.get(tail="N123AB")
        self.assertEqual(d["existing"], {"name": "N123AB (rejected)", "url": reverse("aircraft_detail", args=[a.pk])})
        self.assertIn("Not a US N-number", self.get(tail="C-GABC")["note"])

    def test_admins_only_and_form_has_the_script(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(reverse("admin:tracker_aircraft_add")), reverse("aircraft_lookup"))
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("aircraft_lookup"), {"tail": "N1"}).status_code, 403)

    def test_search_offer_prefills_the_worked_out_hex(self):
        from urllib.parse import parse_qs, urlparse
        r = self.client.get(reverse("aircraft_list"), {"q": "N77MP"})
        q = parse_qs(urlparse(r.context["add_offer"]["url"]).query)
        self.assertEqual((q["tail_number"], q["icao_hex"]), (["N77MP"], [tail_to_hex("N77MP")]))
        self.assertContains(r, "is worked out from the US tail number")
