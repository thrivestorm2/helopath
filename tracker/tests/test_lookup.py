from datetime import datetime, timezone

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.lookup import call_sign_suggestions, lookup_links, program_suggestions
from tracker.models import Aircraft, Leg, Operator, Program, Site

T = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)


class LookupTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Methods")
        self.base = Site.objects.create(name="Base 4", site_type=Site.BASE, lat=40, lon=-89)
        self.lf = Program.objects.create(name="OSF Life Flight")
        self.angels = Program.objects.create(name="Air Angels", operator=self.op)
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         base=self.base, operator=self.op)
        Aircraft.objects.create(tail_number="N912LF", icao_hex="a00002", status=Aircraft.CONFIRMED,
                                base=self.base, program=self.lf)
        for ids in ("LIFE3,N911LF", "LIFE3", "911LF", "lifeg3"):
            Leg.objects.create(hex="a00001", aircraft=self.a, source="adsblol", start_ts=T, end_ts=T, start_lat=40,
                               start_lon=-89, end_lat=40, end_lon=-89, end_state="landed_observed", callsign=ids)
        self.admin = User.objects.create_user("boss", password="x", is_staff=True)
        self.admin.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("v", password="x")
        self.viewer.groups.add(Group.objects.get(name="Viewer"))
        self.url = reverse("aircraft_detail", args=[self.a.pk])
        self.apply = reverse("aircraft_apply", args=[self.a.pk])

    def test_links(self):
        links = dict(lookup_links(self.a))
        self.assertEqual(links["Facebook"], "https://www.facebook.com/search/top?q=N911LF")
        self.assertEqual(links["Instagram"], "https://www.instagram.com/explore/tags/n911lf/")
        self.assertIn("nNumberTxt=911LF", links["FAA registry"])  # FAA wants the number without the N
        self.assertIn("%22N911LF%22", links["Google"])
        self.assertNotIn("X", links)

    def test_suggestions(self):
        self.assertEqual(call_sign_suggestions(self.a, "adsblol"), [("LIFE3", 2), ("LIFEG3", 1)])  # tail left out
        self.assertEqual([(p.name, why) for p, why in program_suggestions(self.a)],
                         [("OSF Life Flight", "1 other aircraft at Base 4"), ("Air Angels", "flown by Air Methods")])
        self.a.program = self.lf
        self.assertEqual([p.name for p, _ in program_suggestions(self.a)], ["Air Angels"])  # current left out

    def test_viewer_sees_links_only(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.url)
        self.assertContains(r, "Look it up")
        self.assertContains(r, 'href="https://www.jetphotos.com/registration/N911LF" target="_blank"')
        self.assertNotContains(r, "Suggested call sign")
        self.assertEqual(self.client.post(self.apply, {"field": "call_sign", "value": "X"}).status_code, 403)

    def test_admin_uses_suggestions(self):
        self.client.force_login(self.admin)
        r = self.client.get(self.url)
        self.assertContains(r, "<strong>LIFE3</strong>")
        self.assertContains(r, "broadcast on 2 legs")
        r = self.client.post(self.apply, {"field": "call_sign", "value": "LIFE3"}, follow=True)
        self.assertRedirects(r, self.url + "#lookup", fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertEqual(self.a.call_sign, "LIFE3")
        self.assertContains(r, "Call sign set to LIFE3.")
        self.assertContains(r, '<span class="badge confirmed">Current</span>')
        self.client.post(self.apply, {"field": "program", "value": self.lf.pk})
        self.a.refresh_from_db()
        self.assertEqual(self.a.program, self.lf)
        self.assertEqual(self.client.post(self.apply, {"field": "program", "value": 999}).status_code, 404)
