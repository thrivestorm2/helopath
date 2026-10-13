from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, Operator, Program, Site

from . import login_viewer

TOGGLE = "Confirmed aircraft only"
PAGES = ["dashboard", "site_list", "missions", "flows", "quality", "unresolved", "site_map"]


class ViewerConfirmedOnlyTests(TestCase):
    """Viewers only ever see confirmed aircraft, with no toggle; admins keep both and the toggle."""

    def setUp(self):
        self.op = Operator.objects.create(name="Air Methods")
        self.prog = Program.objects.create(name="OSF Life Flight", operator=self.op)
        self.site = Site.objects.create(name="Hospital A", lat=40.1, lon=-89.1)
        self.conf = Aircraft.objects.create(tail_number="N1CF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                            operator=self.op, program=self.prog)
        self.disc = Aircraft.objects.create(tail_number="N2DS", icao_hex="a00002", status=Aircraft.DISCOVERED,
                                            operator=self.op, program=self.prog)

    def test_viewers(self):
        login_viewer(self.client)
        for name in PAGES:
            self.assertNotContains(self.client.get(reverse(name)), TOGGLE, msg_prefix=name)
        for url in (reverse("aircraft_list"), reverse("aircraft_list") + "?confirmed=0",
                    reverse("operator_detail", args=[self.op.pk]), reverse("program_detail", args=[self.prog.pk])):
            r = self.client.get(url)
            self.assertContains(r, "N1CF", msg_prefix=url)
            self.assertNotContains(r, "N2DS", msg_prefix=url)
        self.assertNotContains(self.client.get(reverse("aircraft_list")), 'name="status"')
        self.assertEqual(self.client.get(reverse("aircraft_detail", args=[self.disc.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("aircraft_detail", args=[self.conf.pk])).status_code, 200)
        r = self.client.get(reverse("dashboard"))
        self.assertNotContains(r, "awaiting review")
        self.assertNotContains(r, "?confirmed=1")  # no clutter in viewers' links

    def test_admins_unchanged(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        for name in PAGES:
            self.assertContains(self.client.get(reverse(name)), TOGGLE, msg_prefix=name)
        self.assertContains(self.client.get(reverse("aircraft_list")), "N2DS")
        self.assertNotContains(self.client.get(reverse("aircraft_list") + "?confirmed=1"), "N2DS")
        self.assertEqual(self.client.get(reverse("aircraft_detail", args=[self.disc.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("dashboard")), "awaiting review")
