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
        links = {label: url for label, url, _ in lookup_links(self.a)}
        self.assertEqual(links["Facebook search"], "https://www.facebook.com/search/top?q=N911LF")
        self.assertEqual(links["Instagram search"], "https://www.instagram.com/explore/tags/n911lf/")
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
        self.assertNotContains(r, "Suggested call sign")  # set: no more suggestions
        self.assertContains(r, "Suggested program")
        r = self.client.post(self.apply, {"field": "program", "value": self.lf.pk}, follow=True)
        self.a.refresh_from_db()
        self.assertEqual(self.a.program, self.lf)
        self.assertNotContains(r, "Suggested program")
        self.assertEqual(self.client.post(self.apply, {"field": "program", "value": 999}).status_code, 404)

    def test_dismiss_stops_suggestions(self):
        self.client.force_login(self.admin)
        r = self.client.post(self.apply, {"field": "call_sign", "action": "dismiss"}, follow=True)
        self.a.refresh_from_db()
        self.assertTrue(self.a.call_sign_reviewed)
        self.assertEqual(self.a.call_sign, "")  # nothing set, just reviewed
        self.assertContains(r, "No more call sign suggestions")
        self.assertNotContains(r, "Suggested call sign")
        self.assertContains(r, "Suggested program")
        r = self.client.post(self.apply, {"field": "program", "action": "dismiss"}, follow=True)
        self.a.refresh_from_db()
        self.assertTrue(self.a.program_reviewed)
        self.assertNotContains(r, "Suggested program")
        self.assertContains(r, "Look it up")  # search links stay
        self.assertEqual(self.client.post(self.apply, {"field": "notes", "action": "dismiss"}).status_code, 302)
        # The admin edit page can turn suggestions back on.
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        page = self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk]))
        self.assertContains(page, 'name="call_sign_reviewed"')
        self.assertContains(page, 'name="program_reviewed"')

    def test_viewer_cannot_dismiss(self):
        self.client.force_login(self.viewer)
        r = self.client.post(self.apply, {"field": "call_sign", "action": "dismiss"})
        self.assertEqual(r.status_code, 403)
        self.a.refresh_from_db()
        self.assertFalse(self.a.call_sign_reviewed)


class SocialPageTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Methods", facebook_url="https://www.facebook.com/airmethods",
                                          instagram_url="https://www.instagram.com/airmethods")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.op,
                                         facebook_url="https://www.facebook.com/osflifeflight")
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         operator=self.op, program=self.lf)

    def links(self):
        return {label: (url, title) for label, url, title in lookup_links(self.a)}

    def test_aircraft_then_program_then_operator_then_search(self):
        links = self.links()
        self.assertEqual(links["Facebook page"], ("https://www.facebook.com/osflifeflight",
                                                  "The Facebook page set on its program"))
        self.assertEqual(links["Instagram page"], ("https://www.instagram.com/airmethods",
                                                   "The Instagram page set on its operator"))
        self.a.facebook_url = "https://www.facebook.com/life1"
        self.assertEqual(self.links()["Facebook page"][1], "The Facebook page set on this aircraft")
        self.a.facebook_url = ""
        self.a.program = self.a.operator = None
        links = self.links()
        self.assertIn("Facebook search", links)
        self.assertEqual(links["Instagram search"][1], "Search Instagram for N911LF")

    def test_short_forms_expanded_and_other_sites_refused(self):
        from django.core.exceptions import ValidationError
        self.a.facebook_url, self.a.instagram_url = "facebook.com/osflifeflight", "@osflifeflight"
        self.a.full_clean()
        self.assertEqual((self.a.facebook_url, self.a.instagram_url),
                         ("https://facebook.com/osflifeflight", "https://www.instagram.com/osflifeflight"))
        self.a.facebook_url = "https://m.facebook.com/osf"
        self.a.full_clean()  # subdomains of the site are fine
        for bad in ("https://example.com/osf", "javascript:alert(1)", "https://www.facebook.com/"):
            self.a.facebook_url = bad
            with self.assertRaises(ValidationError) as e:
                self.a.full_clean()
            self.assertIn("facebook_url", e.exception.message_dict)
        self.a.facebook_url = ""
        self.a.instagram_url = "https://www.facebook.com/x"  # wrong site for this field
        with self.assertRaises(ValidationError):
            self.a.full_clean()

    def test_page_button_on_aircraft_page_and_program_admin(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        r = self.client.get(reverse("aircraft_detail", args=[self.a.pk]))
        self.assertContains(r, 'href="https://www.facebook.com/osflifeflight" target="_blank" rel="noopener noreferrer" '
                               'title="The Facebook page set on its program">Facebook page</a>')
        page = self.client.get(reverse("admin:tracker_program_change", args=[self.lf.pk]))
        self.assertContains(page, 'name="facebook_url"')
        self.assertContains(page, 'name="instagram_url"')
        self.assertContains(self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk])),
                            'name="facebook_url"')
