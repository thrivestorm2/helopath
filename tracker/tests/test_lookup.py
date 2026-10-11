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
        links = {label: url for label, url, _, _ in lookup_links(self.a)}
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
        self.op = Operator.objects.create(name="Air Methods", facebook="airmethods", instagram="airmethods")
        self.lf = Program.objects.create(name="OSF Life Flight", operator=self.op, facebook="osflifeflight")
        self.a = Aircraft.objects.create(tail_number="N911LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         operator=self.op, program=self.lf)

    def links(self):
        return {label: (url, title) for label, url, title, _ in lookup_links(self.a)}

    def test_aircraft_then_program_then_operator_then_search(self):
        links = self.links()
        self.assertEqual(links["Facebook page"], ("https://www.facebook.com/osflifeflight",
                                                  "The Facebook page set on its program"))
        self.assertEqual(links["Instagram page"], ("https://www.instagram.com/airmethods/",
                                                   "The Instagram page set on its operator"))
        self.a.facebook = "life1"
        self.assertEqual(self.links()["Facebook page"][1], "The Facebook page set on this aircraft")
        self.a.facebook = ""
        self.a.program = self.a.operator = None
        links = self.links()
        self.assertIn("Facebook search", links)
        self.assertEqual(links["Instagram search"][1], "Search Instagram for N911LF")

    def test_whatever_is_typed_is_stored_as_the_username(self):
        from tracker.social import normalize
        for typed in ("https://www.facebook.com/osflifeflight/", "https://m.facebook.com/osflifeflight?ref=page",
                      "facebook.com/osflifeflight", "www.facebook.com/osflifeflight/about", "@osflifeflight",
                      "osflifeflight", " fb.com/osflifeflight "):
            self.assertEqual(normalize("facebook", typed), "osflifeflight", typed)
        for typed in ("https://www.instagram.com/osf.lifeflight/", "instagram.com/osf.lifeflight?hl=en",
                      "@osf.lifeflight", "osf.lifeflight"):
            self.assertEqual(normalize("instagram", typed), "osf.lifeflight", typed)
        self.assertEqual(normalize("facebook", "https://www.facebook.com/profile.php?id=100012345"), "id:100012345")
        self.assertEqual(normalize("facebook", "facebook.com/pages/OSF-Life-Flight/987654"), "id:987654")
        self.a.facebook, self.a.instagram = "https://www.facebook.com/life1/", "@life_1"
        self.a.full_clean()
        self.assertEqual((self.a.facebook, self.a.instagram), ("life1", "life_1"))

    def test_links_are_built_from_one_pattern_per_site(self):
        from tracker.social import page_url
        self.assertEqual(page_url("facebook", "osflifeflight"), "https://www.facebook.com/osflifeflight")
        self.assertEqual(page_url("facebook", "id:100012345"), "https://www.facebook.com/profile.php?id=100012345")
        self.assertEqual(page_url("instagram", "osflifeflight"), "https://www.instagram.com/osflifeflight/")

    def test_other_sites_and_non_pages_refused(self):
        from django.core.exceptions import ValidationError
        for field, bad in (("facebook", "https://example.com/osf"), ("facebook", "javascript:alert(1)"),
                           ("facebook", "https://www.facebook.com/"), ("facebook", "facebook.com/search?q=osf"),
                           ("facebook", "facebook.com/groups/osf"), ("facebook", "osf life flight"),
                           ("instagram", "https://www.facebook.com/osf"), ("instagram", "instagram.com/p/AbC123")):
            setattr(self.a, field, bad)
            with self.assertRaises(ValidationError, msg=bad) as e:
                self.a.full_clean()
            self.assertIn(field, e.exception.message_dict)
            setattr(self.a, field, "")

    def test_saved_links_converted_by_the_migration(self):
        from importlib import import_module

        from django.apps import apps
        Program.objects.filter(pk=self.lf.pk).update(facebook="https://www.facebook.com/osflifeflight/",
                                                     instagram="https://www.instagram.com/osflifeflight?hl=en")
        Operator.objects.filter(pk=self.op.pk).update(facebook="https://example.com/not-facebook")
        import_module("tracker.migrations.0026_social_usernames").to_usernames(apps, None)
        self.lf.refresh_from_db()
        self.op.refresh_from_db()
        self.assertEqual((self.lf.facebook, self.lf.instagram), ("osflifeflight", "osflifeflight"))
        self.assertEqual((self.op.facebook, self.op.instagram), ("", "airmethods"))

    def test_page_button_on_aircraft_page_and_admin_fields(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        r = self.client.get(reverse("aircraft_detail", args=[self.a.pk]))
        self.assertContains(r, '<a class="button social-facebook" href="https://www.facebook.com/osflifeflight" '
                               'target="_blank" rel="noopener noreferrer" title="The Facebook page set on its program">'
                               'Facebook page</a>')  # brand-coloured: an official page
        self.assertContains(r, '<a class="button" href="https://www.google.com/')  # others stay plain
        page = self.client.get(reverse("admin:tracker_program_change", args=[self.lf.pk]))
        self.assertContains(page, 'name="facebook"')
        self.assertContains(page, "Facebook username")
        self.assertContains(self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk])),
                            'name="instagram"')


class ShareLinkTests(TestCase):
    SHARE = "https://www.facebook.com/share/18FNswjSVZ/?mibextid=wwXIfr"

    def norm(self, value, lands_on=None, error=None):
        from unittest import mock

        from tracker import social
        effect = error if error else (lambda url: lands_on)
        with mock.patch.object(social, "resolve_redirect", side_effect=effect) as resolver:
            return social.normalize("facebook", value), resolver

    def test_share_link_followed_to_the_page(self):
        name, resolver = self.norm(self.SHARE, lands_on="https://www.facebook.com/osflifeflight/?mibextid=wwXIfr")
        self.assertEqual(name, "osflifeflight")
        resolver.assert_called_once_with(self.SHARE)
        self.assertEqual(self.norm("https://www.facebook.com/share/19sfnVtSrz/",
                                   lands_on="https://m.facebook.com/profile.php?id=61550000000000")[0], "id:61550000000000")
        self.assertEqual(self.norm("fb.me/abc123", lands_on="https://www.facebook.com/airmethods")[0], "airmethods")
        # A login wall names the page in ?next=.
        self.assertEqual(self.norm(self.SHARE, lands_on="https://www.facebook.com/login/?next=https%3A%2F%2Fwww."
                                                         "facebook.com%2Fosflifeflight%2F")[0], "osflifeflight")

    def test_people_links_never_stored_as_people(self):
        from django.core.exceptions import ValidationError

        from tracker import social
        people = "https://www.facebook.com/people/OSF-Life-Flight/100063512345678/?mibextid=wwXIfr"
        self.assertEqual(social.normalize("facebook", people), "id:100063512345678")
        self.assertEqual(social.normalize("facebook", "facebook.com/people/OSF-Life-Flight/100063512345678"),
                         "id:100063512345678")
        self.assertEqual(self.norm(self.SHARE, lands_on=people)[0], "id:100063512345678")  # share link to one
        self.assertEqual(social.normalize("facebook", "facebook.com/pg/osflifeflight/about/"), "osflifeflight")
        for bad in ("facebook.com/people/OSF-Life-Flight/", "people", "@people", "PEOPLE", "share", "groups"):
            with self.assertRaises(ValidationError, msg=bad):
                social.normalize("facebook", bad)
        with self.assertRaises(ValidationError):
            social.normalize("instagram", "explore")

    def test_migration_clears_reserved_words(self):
        from importlib import import_module

        from django.apps import apps
        op = Operator.objects.create(name="Air Methods", facebook="airmethods")
        prog = Program.objects.create(name="OSF Life Flight", operator=op)
        site = Site.objects.create(name="Peoria Intl", site_type=Site.AIRPORT, lat=40.66, lon=-89.69)
        Program.objects.filter(pk=prog.pk).update(facebook="people", instagram="osflifeflight")
        Site.objects.filter(pk=site.pk).update(facebook="People")
        import_module("tracker.migrations.0028_clear_reserved_social").clear_reserved(apps, None)
        for obj in (op, prog, site):
            obj.refresh_from_db()
        self.assertEqual((prog.facebook, prog.instagram), ("", "osflifeflight"))
        self.assertEqual(site.facebook, "")
        self.assertEqual(op.facebook, "airmethods")  # real pages untouched

    def test_failures_ask_for_the_address_bar_link(self):
        from django.core.exceptions import ValidationError
        for kwargs in ({"error": OSError("blocked")}, {"lands_on": self.SHARE},
                       {"lands_on": "https://www.facebook.com/login/"}):
            with self.assertRaises(ValidationError) as e:
                self.norm(self.SHARE, **kwargs)
            self.assertIn("copy the address bar link", str(e.exception))

    def test_interstitial_page_read_from_its_canonical_link(self):
        import io

        from tracker.social import resolve_redirect

        class Resp(io.BytesIO):
            def geturl(self):
                return "https://www.facebook.com/share/18FNswjSVZ/"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        html = b'<html><head><link rel="canonical" href="https://www.facebook.com/osflifeflight/" /></head></html>'
        self.assertEqual(resolve_redirect(self.SHARE, opener=lambda req, timeout: Resp(html)),
                         "https://www.facebook.com/osflifeflight/")


class SocialIconTests(TestCase):
    def test_icons_on_operator_and_program_pages_only_when_set(self):
        viewer = User.objects.create_user("v3", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        op = Operator.objects.create(name="Air Methods", facebook="airmethods", instagram="airmethods")
        prog = Program.objects.create(name="OSF Life Flight", operator=op)
        r = self.client.get(reverse("operator_detail", args=[op.pk]))
        self.assertContains(r, 'class="social-icon social-facebook" href="https://www.facebook.com/airmethods"')
        self.assertContains(r, 'class="social-icon social-instagram" href="https://www.instagram.com/airmethods/"')
        r = self.client.get(reverse("program_detail", args=[prog.pk]))
        self.assertNotContains(r, "social-icon")  # the operator's pages are not shown on the program
        prog.instagram = "osflifeflight"
        prog.save()
        r = self.client.get(reverse("program_detail", args=[prog.pk]))
        self.assertContains(r, 'href="https://www.instagram.com/osflifeflight/"')
        self.assertNotContains(r, "social-facebook")

    def test_icons_on_site_and_aircraft_pages_only_when_set(self):
        viewer = User.objects.create_user("v4", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        site = Site.objects.create(name="OSF St Francis", site_type=Site.HOSPITAL, lat=40.7, lon=-89.6)
        r = self.client.get(reverse("site_detail", args=[site.pk]))
        self.assertNotContains(r, "social-icon")
        site.facebook, site.instagram = "osfstfrancis", "osf_stfrancis"
        site.save()
        r = self.client.get(reverse("site_detail", args=[site.pk]))
        self.assertContains(r, 'class="social-icon social-facebook" href="https://www.facebook.com/osfstfrancis"')
        self.assertContains(r, 'class="social-icon social-instagram" href="https://www.instagram.com/osf_stfrancis/"')

        prog = Program.objects.create(name="OSF Life Flight", facebook="osflifeflight")
        a = Aircraft.objects.create(tail_number="N911LF", icao_hex="ad077b", status=Aircraft.CONFIRMED, program=prog)
        r = self.client.get(reverse("aircraft_detail", args=[a.pk]))
        self.assertNotContains(r, "social-icon")  # the program's page stays in Look it up, not the header
        a.instagram = "lifeflight1"
        a.save()
        r = self.client.get(reverse("aircraft_detail", args=[a.pk]))
        self.assertContains(r, 'class="social-icon social-instagram" href="https://www.instagram.com/lifeflight1/"')
        self.assertNotContains(r, "social-icon social-facebook")

    def test_site_admin_stores_usernames_and_rejects_other_sites(self):
        admin = User.objects.create_superuser("root2", password="x")
        self.client.force_login(admin)
        site = Site.objects.create(name="Peoria Intl", site_type=Site.AIRPORT, faa_id="PIA", lat=40.66, lon=-89.69)
        data = {"name": site.name, "site_type": site.site_type, "faa_id": "PIA", "city": "", "state": "",
                "lat": site.lat, "lon": site.lon, "elevation_ft": 0, "radius_m": 300, "origin": "manual",
                "is_active": "on", "notes": "", "weather_hangar": "",
                "facebook": "https://www.facebook.com/flypia/?ref=page", "instagram": "@flypia"}
        url = reverse("admin:tracker_site_change", args=[site.pk])
        r = self.client.post(url, data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        site.refresh_from_db()
        self.assertEqual((site.facebook, site.instagram), ("flypia", "flypia"))
        r = self.client.post(url, {**data, "facebook": "https://example.com/flypia"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Enter a Facebook username")
        site.refresh_from_db()
        self.assertEqual(site.facebook, "flypia")
