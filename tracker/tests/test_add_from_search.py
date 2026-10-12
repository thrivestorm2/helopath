from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracker.models import Aircraft, PendingRebuild, RegistryAircraft
from tracker.tests import PERIODS_INLINE


class AddFromSearchTests(TestCase):
    """An admin's aircraft search that finds nothing offers to add it."""

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(self.admin)
        RegistryAircraft.objects.create(n_number="123AB", mode_s_hex="A1B2C3", type_aircraft="6", make="AIRBUS",
                                        model="EC145", registrant_name="LIFE FLIGHT LEASING",
                                        loaded_at=timezone.now())

    def offer(self, q):
        r = self.client.get(reverse("aircraft_list"), {"q": q})
        return r, r.context["add_offer"]

    def test_registry_match_prefills_the_form(self):
        for typed in ("N123AB", "123ab", "a1b2c3"):
            r, offer = self.offer(typed)
            self.assertEqual(offer["registry"].n_number, "123AB", typed)
            q = parse_qs(urlparse(offer["url"]).query)
            self.assertEqual((q["tail_number"], q["icao_hex"], q["status"], q["origin"], q["then"]),
                             (["N123AB"], ["a1b2c3"], ["confirmed"], ["manual"], ["page"]))
        self.assertContains(r, "AIRBUS EC145")
        self.assertContains(r, ">Add aircraft</a>")

    def test_not_in_registry_prefills_what_was_typed(self):
        _, offer = self.offer("N77MP")
        self.assertIsNone(offer["registry"])
        self.assertEqual(parse_qs(urlparse(offer["url"]).query)["tail_number"], ["N77MP"])
        _, offer = self.offer("ABC123")
        self.assertEqual(parse_qs(urlparse(offer["url"]).query)["icao_hex"], ["abc123"])
        r, offer = self.offer("Life Flight")  # not a tail or hex: nothing to offer
        self.assertIsNone(offer)
        self.assertContains(r, "No aircraft found.")

    def test_rejected_aircraft_is_pointed_to_not_duplicated(self):
        a = Aircraft.objects.create(tail_number="N123AB", icao_hex="a1b2c3", status=Aircraft.REJECTED)
        r, offer = self.offer("N123AB")
        self.assertEqual(offer, {"existing": a})
        self.assertContains(r, "was <strong>rejected</strong>")
        self.assertContains(r, reverse("admin:tracker_aircraft_change", args=[a.pk]))
        self.assertNotContains(r, ">Add aircraft</a>")

    def test_viewers_get_no_offer(self):
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        r, offer = self.offer("N123AB")
        self.assertIsNone(offer)
        self.assertNotContains(r, "Add aircraft")

    def test_saving_lands_on_the_aircraft_page_with_registry_details_and_a_recount(self):
        _, offer = self.offer("N123AB")
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.post(offer["url"], {"tail_number": "N123AB", "icao_hex": "A1B2C3", "status": "confirmed",
                                            "origin": "manual", "notes": "", "_save": "Save", **PERIODS_INLINE})
        a = Aircraft.objects.get(tail_number="N123AB")
        self.assertRedirects(r, reverse("aircraft_detail", args=[a.pk]), fetch_redirect_response=False)
        self.assertEqual((a.icao_hex, a.origin, a.status, a.make_model, a.registrant_hint),
                         ("a1b2c3", "manual", "confirmed", "AIRBUS EC145", "LIFE FLIGHT LEASING"))
        self.assertEqual(list(PendingRebuild.objects.values_list("kind", "reason")),
                         [("recount", "aircraft N123AB added")])
