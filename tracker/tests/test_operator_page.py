from tracker.tests import PERIODS_INLINE
from datetime import date, datetime, timezone

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from tracker.models import Aircraft, Leg, MonthlyAggregate, Operator, Site


class OperatorPageTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Evac Lifeteam", parent_company="GMR")
        self.base = Site.objects.create(name="Base 12", site_type=Site.BASE, lat=40, lon=-89)
        self.hosp_a = Site.objects.create(name="Hospital A", lat=40.1, lon=-89.1)
        self.hosp_b = Site.objects.create(name="Hospital B", lat=40.2, lon=-89.2)
        self.a = Aircraft.objects.create(tail_number="N412AE", icao_hex="a4f2c1", status=Aircraft.CONFIRMED,
                                         operator=self.op, base=self.base)
        Aircraft.objects.create(tail_number="N9REJ", icao_hex="a00009", status=Aircraft.REJECTED, operator=self.op)
        for unreviewed in (True, False):
            MonthlyAggregate.objects.create(source="adsblol", operator=self.op, base=self.base,
                                            origin_site=self.hosp_a, destination_site=self.hosp_b,
                                            month=date(2026, 9, 1), label="interfacility", count=7,
                                            mean_confidence=0.9, includes_unreviewed=unreviewed)
            MonthlyAggregate.objects.create(source="adsblol", operator=None, base=None, month=date(2026, 9, 1),
                                            label="unknown", count=3, mean_confidence=0.3,
                                            includes_unreviewed=unreviewed)
        viewer = User.objects.create_user("v", password="x")
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)

    def test_dashboard_links_operator_and_base(self):
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, f'href="{reverse("operator_detail", args=[self.op.pk])}">Air Evac Lifeteam</a>')
        # Hospitals receiving the most patients (the interfacility rows land at Hospital B), not bases.
        self.assertContains(r, "Hospitals receiving the most patients")
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.hosp_b.pk])}">Hospital B</a>')
        self.assertNotContains(r, ">Base 12</a>")
        self.assertEqual([(h["destination_site__name"], h["n"], h["scenes"]) for h in r.context["top_receiving"]],
                         [("Hospital B", 7, None)])
        self.assertContains(r, f'href="{reverse("aircraft_list")}?operator=none" class="muted">(unassigned)</a>')
        r = self.client.get(reverse("dashboard") + "?confirmed=1")
        self.assertContains(r, f'href="{reverse("operator_detail", args=[self.op.pk])}?confirmed=1"')
        self.assertContains(r, f'href="{reverse("aircraft_list")}?operator=none&amp;status=confirmed"')

    def test_operator_page(self):
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertContains(r, "Air Evac Lifeteam")
        self.assertContains(r, "Part of GMR")
        self.assertContains(r, "N412AE")
        self.assertNotContains(r, "N9REJ")  # rejected aircraft never shown
        self.assertContains(r, "2026-09")
        self.assertContains(r, "Interfacility")
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.hosp_a.pk])}">Hospital A</a>')
        self.assertContains(r, f'href="{reverse("site_detail", args=[self.base.pk])}">Base 12</a>')
        self.assertNotContains(r, ">Edit<")  # viewers get no edit link

    def test_aircraft_pages_link_operator_and_base(self):
        url = reverse("operator_detail", args=[self.op.pk])
        self.assertContains(self.client.get(reverse("aircraft_list")), f'href="{url}"')
        detail = self.client.get(reverse("aircraft_detail", args=[self.a.pk]))
        self.assertContains(detail, f'href="{url}"')
        self.assertContains(detail, f'href="{reverse("site_detail", args=[self.base.pk])}"')

    def test_rows_are_tappable(self):
        op_url = reverse("operator_detail", args=[self.op.pk])
        self.assertContains(self.client.get(reverse("dashboard")), f'<tr data-href="{op_url}">')
        self.assertContains(self.client.get(reverse("aircraft_list")),
                            f'<tr data-href="{reverse("aircraft_detail", args=[self.a.pk])}">')
        self.assertContains(self.client.get(op_url), f'<tr data-href="{reverse("site_detail", args=[self.base.pk])}">')
        self.assertContains(self.client.get(reverse("dashboard")), "closest('tr[data-href]')")  # the tap handler

    def test_site_page_links_operators(self):
        loose = Aircraft.objects.create(tail_number="N55UA", icao_hex="a00055", status=Aircraft.DISCOVERED)
        now = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        for a in (self.a, loose):
            Leg.objects.create(hex=a.icao_hex, aircraft=a, source="adsblol", start_ts=now, end_ts=now, start_lat=40,
                               start_lon=-89, end_lat=40.1, end_lon=-89.1, end_site=self.hosp_a,
                               end_state="landed_observed")
        r = self.client.get(reverse("site_detail", args=[self.hosp_a.pk]))
        op_url = reverse("operator_detail", args=[self.op.pk])
        none_url = reverse("aircraft_list") + f"?operator=none&amp;site={self.hosp_a.pk}"
        self.assertContains(r, f'<a href="{op_url}">Air Evac Lifeteam</a>')
        self.assertContains(r, f'<a href="{none_url}">(unassigned)</a>')
        body = r.content.decode()
        self.assertLess(body.index("Air Evac Lifeteam"), body.index("(unassigned)"))  # unassigned last
        self.assertContains(r, "map.fitBounds(ring.getBounds()")  # whole radius in view
        # The unassigned link lists only unassigned aircraft that landed here.
        Aircraft.objects.create(tail_number="N66EL", icao_hex="a00066", status=Aircraft.DISCOVERED)  # landed elsewhere
        r = self.client.get(reverse("aircraft_list") + f"?operator=none&site={self.hosp_a.pk}")
        self.assertContains(r, "N55UA")
        self.assertNotContains(r, "N66EL")
        self.assertNotContains(r, "N412AE")
        self.assertContains(r, "Showing aircraft that landed at")
        self.assertContains(r, 'href="?operator=none">Show all sites</a>')
        r = self.client.get(reverse("aircraft_list") + f"?site={self.hosp_a.pk}")
        self.assertContains(r, "N55UA")
        self.assertContains(r, "N412AE")
        self.assertEqual(self.client.get(reverse("aircraft_list") + "?site=999").status_code, 404)

    def test_site_page_lists_each_operators_aircraft_that_landed_there(self):
        from tracker.models import Program
        prog = Program.objects.create(name="Air Evac 12", operator=self.op)
        b = Aircraft.objects.create(tail_number="N413AE", icao_hex="a4f2c2", status=Aircraft.DISCOVERED,
                                    operator=self.op, call_sign="AE12", program=prog)
        elsewhere = Aircraft.objects.create(tail_number="N414AE", icao_hex="a4f2c3", status=Aircraft.CONFIRMED,
                                            operator=self.op)
        aug, sep = datetime(2026, 8, 3, 15, tzinfo=timezone.utc), datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        for a, ts, site in ((self.a, aug, self.hosp_a), (self.a, sep, self.hosp_a), (b, sep, self.hosp_a),
                            (b, sep, self.hosp_a), (b, aug, self.hosp_a), (elsewhere, sep, self.hosp_b)):
            Leg.objects.create(hex=a.icao_hex, aircraft=a, source="adsblol", start_ts=ts, end_ts=ts, start_lat=40,
                               start_lon=-89, end_lat=site.lat, end_lon=site.lon, end_site=site,
                               end_state="landed_observed")
        r = self.client.get(reverse("site_detail", args=[self.hosp_a.pk]))
        (op_id, name, _, counts, tot, planes), = r.context["rows"]
        self.assertEqual((name, counts, tot), ("Air Evac Lifeteam", [2, 3], 5))
        # Busiest first, same month columns, adding up to the operator row; the one that never landed here is absent.
        self.assertEqual([(a.tail_number, c, t) for a, _, c, t in planes], [("N413AE", [1, 2], 3), ("N412AE", [1, 1], 2)])
        b_url = reverse("aircraft_detail", args=[b.pk])
        self.assertContains(r, f'<tr class="op-aircraft" data-op="1" data-href="{b_url}" hidden><td><a href="{b_url}">'
                               f'AE12 <span class="muted">(N413AE)</span></a> <span class="muted">· Air Evac 12</span>')
        self.assertContains(r, 'class="link op-toggle" aria-expanded="false"')
        self.assertNotContains(r, "N414AE")
        # Confirmed only: the discovered aircraft drops out of the operator row and its list.
        r = self.client.get(reverse("site_detail", args=[self.hosp_a.pk]) + "?confirmed=1")
        (_, _, _, counts, tot, planes), = r.context["rows"]
        self.assertEqual((counts, tot, [a.tail_number for a, *_ in planes]), ([1, 1], 2, ["N412AE"]))

    def test_unassigned_aircraft_filter(self):
        Aircraft.objects.create(tail_number="N55UA", icao_hex="a00055", status=Aircraft.DISCOVERED)
        r = self.client.get(reverse("aircraft_list") + "?operator=none")
        self.assertContains(r, "N55UA")
        self.assertNotContains(r, "N412AE")
        self.assertContains(r, '<option value="none" selected>Unassigned</option>')
        r = self.client.get(reverse("aircraft_list") + "?operator=none&status=confirmed")
        self.assertNotContains(r, "N55UA")

    def test_aircraft_page_edit_link_for_admins_only(self):
        url = reverse("aircraft_detail", args=[self.a.pk])
        edit = f'href="{reverse("admin:tracker_aircraft_change", args=[self.a.pk])}?next=/aircraft/{self.a.pk}/">Edit</a>'
        self.assertNotContains(self.client.get(url), edit)
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(url), edit)

    def test_aircraft_page_lists_most_visited_with_links(self):
        now = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        for site in (self.hosp_a, self.hosp_a, self.hosp_b, self.base):
            Leg.objects.create(hex=self.a.icao_hex, aircraft=self.a, source="adsblol", start_ts=now, end_ts=now,
                               start_lat=40, start_lon=-89, end_lat=site.lat, end_lon=site.lon, end_site=site,
                               end_state="landed_observed")
        r = self.client.get(reverse("aircraft_detail", args=[self.a.pk]))
        a_link = f'<div class="visit"><a href="{reverse("site_detail", args=[self.hosp_a.pk])}">Hospital A</a>'
        b_link = f'<div class="visit"><a href="{reverse("site_detail", args=[self.hosp_b.pk])}">Hospital B</a>'
        self.assertContains(r, a_link)
        self.assertContains(r, b_link)
        self.assertContains(r, "· 2 landings")
        self.assertContains(r, "· 1 landing<")
        body = r.content.decode()
        self.assertLess(body.index(a_link), body.index(b_link))  # busiest first
        self.assertNotIn('<div class="visit"><a href="' + reverse("site_detail", args=[self.base.pk]), body)

    def test_admin_edit_page_lists_top_hospitals_one_per_line(self):
        Aircraft.objects.filter(pk=self.a.pk).update(top_hospitals_hint="Hospital A (3); St. John's <B> (1)")
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk]))
        self.assertContains(r, "Hospital A (3)<br>St. John&#x27;s &lt;B&gt; (1)")  # one per line, escaped
        self.assertContains(r, "Top hospitals")

    def test_admin_save_returns_to_the_page_edit_was_clicked_on(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        page = reverse("aircraft_detail", args=[self.a.pk])
        change = reverse("admin:tracker_aircraft_change", args=[self.a.pk])
        data = {"status": "confirmed", "notes": "checked", **PERIODS_INLINE}
        r = self.client.post(f"{change}?next={page}", {**data, "_save": "Save"})
        self.assertRedirects(r, page, fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertEqual(self.a.notes, "checked")
        r = self.client.post(f"{change}?next={page}", {**data, "_continue": "Save and continue"})
        self.assertIn(f"next=%2Faircraft%2F{self.a.pk}%2F", r["Location"])  # kept for the next save
        # Without next, or pointing off-site, Save goes to the admin list as usual.
        changelist = reverse("admin:tracker_aircraft_changelist")
        r = self.client.post(change, {**data, "_save": "Save"})
        self.assertTrue(r["Location"].startswith(changelist))
        r = self.client.post(f"{change}?next=https://evil.example/", {**data, "_save": "Save"})
        self.assertTrue(r["Location"].startswith(changelist))
        # The other pages' Edit links carry next too.
        op_page = reverse("operator_detail", args=[self.op.pk])
        self.assertContains(self.client.get(op_page),
                            f'{reverse("admin:tracker_operator_change", args=[self.op.pk])}?next={op_page}"')
        r = self.client.post(f'{reverse("admin:tracker_operator_change", args=[self.op.pk])}?next={op_page}',
                             {"name": "Air Evac Lifeteam", "parent_company": "GMR", "notes": "", "_save": "Save"})
        self.assertRedirects(r, op_page, fetch_redirect_response=False)

    def test_call_sign_shown_with_tail_and_sorts_lists(self):
        Aircraft.objects.filter(pk=self.a.pk).update(call_sign="Zulu Flight")
        b = Aircraft.objects.create(tail_number="N999ZZ", icao_hex="a00999", status=Aircraft.CONFIRMED,
                                    operator=self.op, call_sign="alpha 1")
        Aircraft.objects.create(tail_number="N5MM", icao_hex="a00555", status=Aircraft.CONFIRMED, operator=self.op)
        r = self.client.get(reverse("aircraft_detail", args=[b.pk]))
        self.assertContains(r, '<h1>alpha 1 <span class="muted">(N999ZZ)</span>')
        for url in (reverse("aircraft_list"), reverse("operator_detail", args=[self.op.pk])):
            body = self.client.get(url).content.decode()
            order = [body.index(s) for s in (">alpha 1 (N999ZZ)<", ">N5MM<", ">Zulu Flight (N412AE)<")]
            self.assertEqual(order, sorted(order), url)  # by call sign, else tail; case-insensitive
        body = self.client.get(reverse("aircraft_list") + "?sort=-name").content.decode()
        self.assertLess(body.index("Zulu Flight"), body.index("alpha 1"))
        self.assertContains(self.client.get(reverse("aircraft_list") + "?q=zulu"), "Zulu Flight (N412AE)")
        self.assertEqual(str(b), "alpha 1 (N999ZZ)")

    def test_call_sign_editable_in_admin(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        change = reverse("admin:tracker_aircraft_change", args=[self.a.pk])
        self.assertContains(self.client.get(change), 'name="call_sign"')
        self.client.post(change, {"status": "confirmed", "call_sign": "Life Flight 3", "_save": "Save", **PERIODS_INLINE})
        self.a.refresh_from_db()
        self.assertEqual(self.a.call_sign, "Life Flight 3")

    def test_login_required_and_404(self):
        self.assertEqual(self.client.get(reverse("operator_detail", args=[999])).status_code, 404)
        self.client.logout()
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertEqual(r.status_code, 302)


class SiteEditMapTests(TestCase):
    def test_edit_page_has_radius_slider_fitted_to_circle(self):
        site = Site.objects.create(name="Hospital A", lat=40.1, lon=-89.1, radius_m=450)
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:tracker_site_change", args=[site.pk]))
        self.assertContains(r, 'id="radius-slider"')
        self.assertContains(r, 'id="id_radius_m"')
        self.assertContains(r, "map.fitBounds(circle.getBounds()")
