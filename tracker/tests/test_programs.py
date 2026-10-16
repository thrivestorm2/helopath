from datetime import datetime, timezone

from django.contrib.auth.models import Group, User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from tracker.tests import login_viewer

from tracker.models import Aircraft, Mission, Operator, Program, Site
from tracker.tests import test_operator


class ProgramTests(TestCase):
    def setUp(self):
        self.op = Operator.objects.create(name="Air Methods")
        self.program = Program.objects.create(name="OSF Life Flight", operator=self.op)
        self.a = Aircraft.objects.create(tail_number="N1LF", icao_hex="a00001", status=Aircraft.CONFIRMED,
                                         operator=self.op, program=self.program)
        self.d = Aircraft.objects.create(tail_number="N2LF", icao_hex="a00002", status=Aircraft.DISCOVERED,
                                         program=self.program)
        self.other = Aircraft.objects.create(tail_number="N3XX", icao_hex="a00003", status=Aircraft.CONFIRMED, operator=test_operator())
        hosp_a = Site.objects.create(name="Hospital A", lat=40.1, lon=-89.1)
        hosp_b = Site.objects.create(name="Hospital B", lat=40.2, lon=-89.2)
        ts = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)
        for a in (self.a, self.d, self.other):
            Mission.objects.create(aircraft=a, source="adsblol", start_ts=ts, end_ts=ts, label="interfacility",
                                   origin_site=hosp_a, destination_site=hosp_b, confidence=0.9, rule_fired="t",
                                   program=a.program, operator=a.operator)  # the period's, as the pipeline sets
        self.viewer = User.objects.create_user("v", password="x", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Admin"))  # admins see discovered aircraft too
        self.client.force_login(self.viewer)

    def test_program_page(self):
        url = reverse("program_detail", args=[self.program.pk])
        r = self.client.get(url)
        self.assertContains(r, "OSF Life Flight")
        self.assertContains(r, f'flown by <a href="{reverse("operator_detail", args=[self.op.pk])}">Air Methods</a>')
        self.assertContains(r, "N1LF")
        self.assertContains(r, "N2LF")
        self.assertNotContains(r, "N3XX")
        self.assertContains(r, "2 inferred missions")
        self.assertContains(r, "2026-09")
        self.assertContains(r, ">Hospital A</a>")
        r = self.client.get(url + "?confirmed=1")
        self.assertNotContains(r, "N2LF")
        self.assertContains(r, "1 inferred missions")
        # Viewers: confirmed aircraft only, with no toggle and no Edit link.
        login_viewer(self.client)
        r = self.client.get(url)
        self.assertNotContains(r, "N2LF")
        self.assertContains(r, "1 inferred missions")
        self.assertNotContains(r, ">Edit<")
        self.assertNotContains(r, "Confirmed aircraft only")
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.assertContains(self.client.get(url),
                            f'href="{reverse("admin:tracker_program_change", args=[self.program.pk])}?next={url}">Edit</a>')

    def test_linked_from_aircraft_operator_and_list(self):
        link = f'href="{reverse("program_detail", args=[self.program.pk])}'
        self.assertContains(self.client.get(reverse("aircraft_detail", args=[self.a.pk])), link)
        self.assertContains(self.client.get(reverse("operator_detail", args=[self.op.pk])), link)
        r = self.client.get(reverse("aircraft_list"))
        self.assertContains(r, link)
        self.assertContains(r, f'<option value="{self.program.pk}" >OSF Life Flight</option>')

    def test_operator_page_groups_aircraft_by_program(self):
        Aircraft.objects.create(tail_number="N4NP", icao_hex="a00004", status=Aircraft.CONFIRMED, operator=self.op)
        Program.objects.create(name="Air Angels", operator=self.op)  # no aircraft yet
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertContains(r, "Aircraft by program")
        body = r.content.decode()
        order = [body.index(s) for s in ("No program", "N4NP", ">Air Angels</a>", ">OSF Life Flight</a>", "N1LF")]
        self.assertEqual(order, sorted(order))  # no program first, then programs by name, each with its aircraft
        # No program starts open; programs start collapsed; an empty program cannot be expanded.
        self.assertContains(r, '<details class="program-group" open>\n    <summary><span class="muted">No program</span>'
                               ' <span class="muted">· 1 aircraft</span>', html=False)
        self.assertEqual(body.count('<details class="program-group">'), 1)  # OSF Life Flight, collapsed
        self.assertContains(r, '<span class="muted">· 0 aircraft</span></div>')  # Air Angels
        self.assertContains(r, ">Expand all</button>")
        # A long no-program list starts collapsed too.
        for i in range(10):
            Aircraft.objects.create(tail_number=f"N9{i}NP", icao_hex=f"a0009{i}", status=Aircraft.CONFIRMED,
                                    operator=self.op)
        r = self.client.get(reverse("operator_detail", args=[self.op.pk]))
        self.assertContains(r, "· 11 aircraft")
        self.assertNotContains(r, '<details class="program-group" open>')
        # An operator without programs keeps the plain list.
        plain = Operator.objects.create(name="Air Evac Lifeteam")
        self.other.operator = plain
        self.other.save()
        r = self.client.get(reverse("operator_detail", args=[plain.pk]))
        self.assertNotContains(r, "by program")
        self.assertNotContains(r, "No program")
        self.assertContains(r, "N3XX")

    def test_aircraft_list_program_filter_and_search(self):
        r = self.client.get(reverse("aircraft_list") + f"?program={self.program.pk}")
        self.assertContains(r, "N1LF")
        self.assertNotContains(r, "N3XX")
        r = self.client.get(reverse("aircraft_list") + "?program=none")
        self.assertContains(r, "N3XX")
        self.assertNotContains(r, "N1LF")
        r = self.client.get(reverse("aircraft_list") + "?q=life+flight")
        self.assertContains(r, "N1LF")
        self.assertNotContains(r, "N3XX")
        self.assertEqual(self.client.get(reverse("aircraft_list") + "?sort=program__name").status_code, 200)

    def test_review_card_links_or_adds_program(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("review_queue")), 'list="program-options"')
        url = reverse("review_action", args=[self.d.pk])
        self.client.post(url, {"action": "program", "program_name": " osf  life flight "})  # existing, any case
        self.assertEqual(Program.objects.count(), 1)
        self.d.operator = self.op
        self.d.save()
        self.client.post(url, {"action": "program", "program_name": "Survival Flight"})  # new
        self.d.refresh_from_db()
        self.assertEqual(self.d.program.name, "Survival Flight")
        self.assertEqual(self.d.program.operator, self.op)  # new program takes the aircraft's operator
        self.client.post(url, {"action": "program", "program_name": ""})
        self.d.refresh_from_db()
        self.assertIsNone(self.d.program)
        self.assertEqual(self.client.post(url, {"action": "program", "program_name": "X"}).status_code, 302)

    def test_viewers_cannot_set_program(self):
        login_viewer(self.client)
        r = self.client.post(reverse("review_action", args=[self.d.pk]), {"action": "program", "program_name": "X"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Program.objects.filter(name="X").exists())

    def test_admin_group_deletes_only_empty_programs(self):
        admin = User.objects.create_user("boss", password="x", is_staff=True)
        admin.groups.add(Group.objects.get(name="Admin"))
        self.assertTrue(admin.has_perm("tracker.delete_program"))
        self.assertFalse(admin.has_perm("tracker.delete_aircraft"))  # everything else stays deactivate-only
        self.client.force_login(admin)
        url = reverse("admin:tracker_program_delete", args=[self.program.pk])
        self.assertNotContains(self.client.get(reverse("program_detail", args=[self.program.pk])), f'href="{url}"')
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)  # still has aircraft
        self.assertTrue(Program.objects.filter(pk=self.program.pk).exists())
        empty = Program.objects.create(name="Empty Program")
        empty_url = reverse("admin:tracker_program_delete", args=[empty.pk])
        self.assertContains(self.client.get(reverse("program_detail", args=[empty.pk])), f'href="{empty_url}"')
        self.client.post(empty_url, {"post": "yes"})
        self.assertFalse(Program.objects.filter(pk=empty.pk).exists())

    def test_superuser_deletes_program_and_keeps_its_aircraft(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        url = reverse("admin:tracker_program_delete", args=[self.program.pk])
        self.assertContains(self.client.get(reverse("program_detail", args=[self.program.pk])), f'href="{url}"')
        self.assertContains(self.client.get(url), "Are you sure")
        self.client.post(url, {"post": "yes"})
        self.assertFalse(Program.objects.filter(pk=self.program.pk).exists())
        self.a.refresh_from_db()
        self.d.refresh_from_db()
        self.assertIsNone(self.a.program)
        self.assertEqual(self.a.operator, self.op)  # everything else about the aircraft is untouched
        self.assertTrue(Aircraft.objects.filter(pk=self.d.pk).exists())
        self.assertTrue(Operator.objects.filter(pk=self.op.pk).exists())

    def test_admin_pages(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:tracker_program_changelist"))
        self.assertContains(r, "OSF Life Flight")
        self.assertEqual(self.client.get(reverse("admin:tracker_program_add")).status_code, 200)
        self.assertContains(self.client.get(reverse("admin:tracker_aircraft_change", args=[self.a.pk])),
                            'name="program"')


class ProgramMigrationTests(TransactionTestCase):
    """Typed program names become linked Program rows."""

    def test_names_become_programs(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("tracker", "0008_placesuggestion")])
        apps = executor.loader.project_state([("tracker", "0008_placesuggestion")]).apps
        OldAircraft, OldOperator = apps.get_model("tracker", "Aircraft"), apps.get_model("tracker", "Operator")
        op = OldOperator.objects.create(name="Air Methods")
        OldAircraft.objects.create(tail_number="N1", icao_hex="b00001", program_name="OSF Life Flight", operator=op)
        OldAircraft.objects.create(tail_number="N2", icao_hex="b00002", program_name=" osf life  flight", operator=op)
        OldAircraft.objects.create(tail_number="N3", icao_hex="b00003", program_name="Shared", operator=op)
        OldAircraft.objects.create(tail_number="N4", icao_hex="b00004", program_name="Shared")
        OldAircraft.objects.create(tail_number="N5", icao_hex="b00005")
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([("tracker", "0016_rule_program")])
        apps = executor.loader.project_state([("tracker", "0016_rule_program")]).apps
        P, A = apps.get_model("tracker", "Program"), apps.get_model("tracker", "Aircraft")
        self.assertEqual(sorted(P.objects.values_list("name", flat=True)), ["OSF Life Flight", "Shared"])
        lf = P.objects.get(name="OSF Life Flight")
        self.assertEqual(lf.operator.name, "Air Methods")
        self.assertEqual(set(lf.aircraft.values_list("tail_number", flat=True)), {"N1", "N2"})
        self.assertIsNone(P.objects.get(name="Shared").operator)  # operators disagree (one is blank)
        self.assertIsNone(A.objects.get(tail_number="N5").program)
        # 0017 then fills the known blanks: Shared's only known operator, and N4 with it.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes("tracker"))
        self.assertEqual(Program.objects.get(name="Shared").operator.name, "Air Methods")
        self.assertEqual(Aircraft.objects.get(tail_number="N4").operator.name, "Air Methods")
