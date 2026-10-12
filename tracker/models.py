"""Data model (spec section 9).

Ownership rule: fields prefixed `inferred_` plus the evidence and hint fields on
Aircraft are written by automation; `operator`, `base`, `status` and `notes` belong
to admins and are never written by discovery or rebuild.
"""
from django.db import models
from django.db.models import Value
from django.db.models.functions import Coalesce, Lower, NullIf

from .social import SocialPagesMixin

NO_PATIENT_INFO = "Do not record patient information of any kind here."

SOURCE_CHOICES = [("adsblol", "adsb.lol"), ("adsbx", "ADS-B Exchange sample")]


class Operator(SocialPagesMixin, models.Model):
    name = models.CharField(max_length=200, unique=True)
    parent_company = models.CharField(max_length=200, blank=True)
    facebook = models.CharField("Facebook username", max_length=120, blank=True,
                                help_text="e.g. osflifeflight. Pasting the page's link is fine: only the username is "
                                          "kept. Used by Look it up.")
    instagram = models.CharField("Instagram username", max_length=120, blank=True,
                                 help_text="e.g. osflifeflight. Pasting the profile link or @username is fine: only "
                                           "the username is kept. Used by Look it up.")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Program(SocialPagesMixin, models.Model):
    """A flight program: the service name on the aircraft (e.g. a hospital-branded program), flown by an operator."""

    name = models.CharField(max_length=200, unique=True)
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL, related_name="programs",
                                 help_text="The company that flies it, if known.")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)
    facebook = models.CharField("Facebook username", max_length=120, blank=True,
                                help_text="e.g. osflifeflight. Pasting the page's link is fine: only the username is "
                                          "kept. Used by Look it up.")
    instagram = models.CharField("Instagram username", max_length=120, blank=True,
                                 help_text="e.g. osflifeflight. Pasting the profile link or @username is fine: only "
                                           "the username is kept. Used by Look it up.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.operators_filled = self.fill_aircraft_operators()

    def fill_aircraft_operators(self):
        """Aircraft in this program with no operator get the program's operator. Returns how many changed."""
        if not self.operator_id:
            return 0
        return self.aircraft.filter(operator__isnull=True).update(operator_id=self.operator_id)


class Site(SocialPagesMixin, models.Model):
    HOSPITAL = "hospital_helipad"
    BASE = "base"
    AIRPORT = "airport"
    OTHER = "other"
    TYPE_CHOICES = [(HOSPITAL, "Hospital helipad"), (BASE, "Base"), (AIRPORT, "Airport"), (OTHER, "Other")]
    ORIGIN_CHOICES = [("faa_seed", "FAA seed"), ("manual", "Manual"), ("from_cluster", "From landing cluster")]

    name = models.CharField(max_length=200)
    site_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=HOSPITAL)
    faa_id = models.CharField("FAA ID", max_length=10, blank=True, db_index=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=2, blank=True)
    lat = models.FloatField()
    lon = models.FloatField()
    elevation_ft = models.FloatField(default=0)
    radius_m = models.PositiveIntegerField(default=300)
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default="manual")
    reviewed = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    weather_hangar = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="hangar_for",
        help_text="For a base: where its aircraft shelter in bad weather (usually an airport). Aircraft based here "
                  "treat it as a second home, so flights from and to it are not lost. Choose the base itself when "
                  "its aircraft are hangared there.")
    facebook = models.CharField("Facebook username", max_length=120, blank=True,
                                help_text="e.g. osfstfrancis. Pasting the page's link is fine: only the username is "
                                          "kept. Shown at the top of the site's page.")
    instagram = models.CharField("Instagram username", max_length=120, blank=True,
                                 help_text="e.g. osfstfrancis. Pasting the profile link or @username is fine: only "
                                           "the username is kept. Shown at the top of the site's page.")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["state", "city", "name"]

    def __str__(self):
        return f"{self.name} ({self.faa_id})" if self.faa_id else self.name


class FaaFacility(models.Model):
    """FAA NASR airport/heliport row, nationwide, kept so typing an FAA ID on a site can fill it in.
    Replaced each time FAA sites are seeded (Operations page)."""

    faa_id = models.CharField("FAA ID", max_length=10, unique=True)
    name = models.CharField(max_length=200)
    site_type = models.CharField(max_length=20)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=2, blank=True)
    lat = models.FloatField()
    lon = models.FloatField()
    elevation_ft = models.FloatField(default=0)
    facility_use = models.CharField(max_length=10, blank=True)

    class Meta:
        verbose_name_plural = "FAA facilities"

    def __str__(self):
        return f"{self.faa_id} {self.name}"


class RegistryAircraft(models.Model):
    """FAA releasable registry row. Internal only: never shown in the UI (guardrail 8)."""

    n_number = models.CharField(max_length=6, unique=True)
    mode_s_hex = models.CharField(max_length=6, db_index=True, blank=True)
    type_aircraft = models.CharField(max_length=1, db_index=True)
    model_code = models.CharField(max_length=7, blank=True)
    make = models.CharField(max_length=40, blank=True)
    model = models.CharField(max_length=40, blank=True)
    registrant_name = models.CharField(max_length=60, blank=True)
    status_code = models.CharField(max_length=2, blank=True)
    loaded_at = models.DateTimeField()

    @property
    def tail_number(self):
        return f"N{self.n_number}"


class Aircraft(SocialPagesMixin, models.Model):
    DISCOVERED, CONFIRMED, REJECTED = "discovered", "confirmed", "rejected"
    STATUS_CHOICES = [(DISCOVERED, "Discovered"), (CONFIRMED, "Confirmed"), (REJECTED, "Rejected")]
    ORIGIN_CHOICES = [("auto", "Auto-discovered"), ("manual", "Manual")]
    VISIBLE = (DISCOVERED, CONFIRMED)

    tail_number = models.CharField(max_length=10, unique=True)
    icao_hex = models.CharField(max_length=6, unique=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DISCOVERED, db_index=True)
    origin = models.CharField(max_length=10, choices=ORIGIN_CHOICES, default="auto")

    # Admin-owned.
    call_sign = models.CharField(
        max_length=60, blank=True,
        help_text="Your own name for this aircraft (e.g. Life Flight 3). Lists sort by it and show the tail "
                  "number in parentheses.")
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL)
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL, related_name="aircraft")
    # Suggestions on the aircraft page stop once the value is set or an admin dismisses them.
    call_sign_reviewed = models.BooleanField("call sign suggestions dismissed", default=False)
    program_reviewed = models.BooleanField("program suggestions dismissed", default=False)
    base = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="based_aircraft")
    active_from = models.DateField(
        null=True, blank=True, help_text="Start of the current service period (blank: since tracking began).")
    active_to = models.DateField(
        null=True, blank=True,
        help_text="Last day served at this base. Saving closes this period into the service history and starts a "
                  "new one the next day with the same operator and program but no base or call sign (or tick "
                  "Retired to end service).")
    retired = models.BooleanField(
        default=False, help_text="Out of service after 'Active until': no new period, flights after it not counted. "
                                 "Untick to start a new period the day after.")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)
    facebook = models.CharField("Facebook username", max_length=120, blank=True,
                                help_text="e.g. osflifeflight. Pasting the page's link is fine: only the username is "
                                          "kept. Used by Look it up.")
    instagram = models.CharField("Instagram username", max_length=120, blank=True,
                                 help_text="e.g. osflifeflight. Pasting the profile link or @username is fine: only "
                                           "the username is kept. Used by Look it up.")

    # Automation-owned.
    inferred_base = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="inferred_based_aircraft"
    )
    inferred_base_lat = models.FloatField(null=True, blank=True)
    inferred_base_lon = models.FloatField(null=True, blank=True)
    make_model = models.CharField(max_length=100, blank=True)
    registrant_hint = models.CharField(max_length=200, blank=True)
    callsign_hint = models.CharField(max_length=100, blank=True)
    operator_hint = models.CharField(max_length=200, blank=True, help_text="readsb ownOp field")
    top_hospitals_hint = models.CharField(max_length=500, blank=True)
    qualifying_visits = models.PositiveIntegerField(default=0)
    distinct_hospitals = models.PositiveIntegerField(default=0)
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(null=True, blank=True)
    discovery_source = models.CharField(max_length=10, choices=SOURCE_CHOICES, blank=True)
    discovery_rule = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["tail_number"]
        verbose_name_plural = "aircraft"

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        if self.fill_operator_from_program() and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], "operator"}
        super().save(*args, **kwargs)

    def fill_operator_from_program(self):
        """No operator yet, but the program has one: use it. Never replaces an operator already set."""
        if self.operator_id or not self.program_id:
            return False
        operator_id = Program.objects.filter(pk=self.program_id).values_list("operator_id", flat=True).first()
        if operator_id:
            self.operator_id = operator_id
            return True
        return False

    @property
    def display_name(self):
        """"Life Flight 3 (N911LF)" when a call sign is set, else the tail number."""
        return f"{self.call_sign} ({self.tail_number})" if self.call_sign else self.tail_number

    @staticmethod
    def sort_name():
        """Order by call sign, or the tail number for aircraft without one (case-insensitive)."""
        return Lower(Coalesce(NullIf("call_sign", Value("")), "tail_number"))

    @property
    def top_hospitals_list(self):
        return [h for h in self.top_hospitals_hint.split("; ") if h]

    @property
    def effective_base(self):
        return self.base or self.inferred_base

    @property
    def base_is_inferred(self):
        return self.base_id is None and self.inferred_base_id is not None


class IngestRun(models.Model):
    OK, FAILED, RUNNING = "ok", "failed", "running"
    day = models.DateField(db_index=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    release_used = models.CharField(max_length=200, blank=True)
    members_scanned = models.PositiveIntegerField(default=0)
    candidates_kept = models.PositiveIntegerField(default=0)
    bytes_streamed = models.BigIntegerField(default=0)
    bytes_retained = models.BigIntegerField(default=0)
    duration_s = models.FloatField(default=0)
    status = models.CharField(max_length=10, default=RUNNING)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-day", "-started_at"]


class ServicePeriod(models.Model):
    """A closed period of an aircraft's service: its base, call sign, operator and program between two dates.
    The current period lives on the Aircraft itself (active_from .. active_to); closing it moves it here."""

    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE, related_name="past_periods")
    start = models.DateField(null=True, blank=True, help_text="Blank: since tracking began.")
    end = models.DateField()
    base = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    base_inferred = models.BooleanField(default=False, help_text="The base was inferred, not set by an admin.")
    base_lat = models.FloatField(null=True, blank=True, help_text="Inferred base point when it was not a site.")
    base_lon = models.FloatField(null=True, blank=True)
    call_sign = models.CharField(max_length=60, blank=True)
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)

    class Meta:
        ordering = ["-end"]

    def __str__(self):
        return f"{self.aircraft.tail_number} {self.start or '…'} to {self.end}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.start and self.end and self.start > self.end:
            raise ValidationError("The period ends before it starts.")
        if not self.aircraft_id:
            return
        a = self.aircraft
        if a.active_from and self.end >= a.active_from:
            raise ValidationError(f"Must end before the current period starts ({a.active_from}).")
        for other in ServicePeriod.objects.filter(aircraft=a).exclude(pk=self.pk):
            if (self.start is None or other.end >= self.start) and (other.start is None or self.end >= other.start):
                raise ValidationError(f"Overlaps the period {other.start or '…'} to {other.end}.")


class Mission(models.Model):
    LABELS = [
        ("interfacility", "Interfacility"),
        ("scene", "Scene"),
        ("single_hospital_unresolved", "Single hospital (unresolved)"),
        ("reposition", "Reposition"),
        ("unknown", "Unknown"),
    ]
    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE, related_name="missions")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    start_ts = models.DateTimeField()
    end_ts = models.DateTimeField()
    label = models.CharField(max_length=30, choices=LABELS, db_index=True)
    origin_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    destination_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confidence = models.FloatField()
    rule_fired = models.CharField(max_length=200)
    # The aircraft's service period on the mission date (its base, operator and program then).
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    base = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-start_ts"]


class Leg(models.Model):
    LANDED, INFERRED, LOST = "landed_observed", "landing_inferred", "signal_lost"
    END_STATES = [(LANDED, "Landed (observed)"), (INFERRED, "Landing inferred"), (LOST, "Signal lost")]

    hex = models.CharField(max_length=6, db_index=True)
    aircraft = models.ForeignKey(Aircraft, null=True, blank=True, on_delete=models.SET_NULL, related_name="legs")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    start_ts = models.DateTimeField(db_index=True)
    end_ts = models.DateTimeField()
    start_lat = models.FloatField()
    start_lon = models.FloatField()
    end_lat = models.FloatField()
    end_lon = models.FloatField()
    start_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.PROTECT, related_name="departures")
    end_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.PROTECT, related_name="arrivals")
    end_state = models.CharField(max_length=20, choices=END_STATES)
    end_alt_ft = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(default=0)
    simplified_track = models.JSONField(default=list)
    start_on_ground = models.BooleanField(default=False)
    gaps_bridged = models.PositiveSmallIntegerField(
        default=0, help_text="Coverage gaps inside this leg that were rejoined (same heading and speed).")
    callsign = models.CharField(max_length=50, blank=True)
    mission = models.ForeignKey(Mission, null=True, blank=True, on_delete=models.SET_NULL, related_name="legs")
    mission_seq = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["start_ts"]
        indexes = [models.Index(fields=["source", "hex", "start_ts"])]

    @property
    def resolved(self):
        return self.end_site_id is not None


class LandingCluster(models.Model):
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    lat = models.FloatField()
    lon = models.FloatField()
    landing_count = models.PositiveIntegerField()
    distinct_aircraft = models.PositiveIntegerField()
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    nearest_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    nearest_site_distance_m = models.FloatField(null=True, blank=True)
    dismissed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-landing_count"]


class DismissedClusterMemory(models.Model):
    """Admin dismissals keyed by location, so they survive a rebuild (guardrail 3)."""

    lat = models.FloatField()
    lon = models.FloatField()
    dismissed_at = models.DateTimeField(auto_now_add=True)


class CoverageCell(models.Model):
    """One grid cell where ADS-B positions were received (derived; rebuilt nightly by the pipeline)."""

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    lat_i = models.IntegerField(help_text="floor(lat / cell size)")
    lon_i = models.IntegerField(help_text="floor(lon / cell size)")
    legs = models.PositiveIntegerField(help_text="Legs received in this cell")
    lowest_ft = models.FloatField(null=True, blank=True, help_text="Lowest altitude received here (feet, MSL)")

    class Meta:
        unique_together = [("source", "lat_i", "lon_i")]


class MonthlyAggregate(models.Model):
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, db_index=True)
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    base = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    origin_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    destination_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    month = models.DateField(db_index=True)
    label = models.CharField(max_length=30, choices=Mission.LABELS)
    count = models.PositiveIntegerField()
    mean_confidence = models.FloatField()
    includes_unreviewed = models.BooleanField(db_index=True)

    class Meta:
        ordering = ["-month", "-count"]


class RebuildRequest(models.Model):
    """Queued by the 'rebuild' button; processed by the nightly job."""

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="adsblol")
    requested_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)


class PendingRebuild(models.Model):
    """An edit waiting to be applied (tracker.rebuild_flag): a recount, a partial rebuild around a site's location,
    or a full rebuild. Recounts and partials run on their own shortly after the last edit; full ones overnight."""

    RECOUNT, PARTIAL, FULL = "recount", "partial", "full"
    KINDS = [(RECOUNT, "Recount"), (PARTIAL, "Partial rebuild"), (FULL, "Full rebuild")]

    kind = models.CharField(max_length=10, choices=KINDS)
    reason = models.CharField(max_length=200)
    lat = models.FloatField(null=True, blank=True)       # partial: a site location (old or new)
    lon = models.FloatField(null=True, blank=True)
    radius_m = models.FloatField(null=True, blank=True)  # that site's radius there
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["created_at"]


class RebuildRun(models.Model):
    """One recount, partial or full rebuild as it ran, with how long each step took (Operations page)."""

    kind = models.CharField(max_length=10, choices=PendingRebuild.KINDS)
    reason = models.CharField(max_length=300, blank=True)
    aircraft_rebuilt = models.PositiveIntegerField(null=True, blank=True)  # aircraft whose legs were re-derived
    steps = models.JSONField(default=list)  # [[step, seconds], ...] in order
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def seconds(self):
        return (self.finished_at - self.started_at).total_seconds() if self.finished_at else None


class CoverageComparison(models.Model):
    """Milestone 8: per-source figures on the same day, for deciding whether to buy data."""

    day = models.DateField()
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    legs = models.PositiveIntegerField()
    resolved_legs = models.PositiveIntegerField()
    qualifying_visits = models.PositiveIntegerField()
    aircraft_with_visits = models.PositiveIntegerField()
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("day", "source")]
        ordering = ["day", "source"]

    @property
    def resolved_rate(self):
        return self.resolved_legs / self.legs if self.legs else None


class RawTrace(models.Model):
    """One retained readsb trace (gzip bytes, as downloaded) for one aircraft and UTC day.

    Kept in the database rather than on disk so the app runs on hosts without a persistent
    filesystem. Only candidate rotorcraft inside the bounding box are kept (about 3 MB/day).
    """

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    day = models.DateField()
    hex = models.CharField(max_length=6)
    data = models.BinaryField()
    size = models.PositiveIntegerField()

    class Meta:
        unique_together = [("source", "day", "hex")]
        indexes = [models.Index(fields=["source", "hex", "day"])]


class OpsTask(models.Model):
    """A chore queued from the Operations page and run by the background worker."""

    LOAD_REGISTRY, SEED_SITES, BACKFILL, REBUILD = "load_registry", "seed_sites", "backfill", "rebuild"
    REFRESH = "refresh"  # queued by review decisions: missions, clusters and aggregates only
    KINDS = [
        (LOAD_REGISTRY, "Load FAA registry"),
        (SEED_SITES, "Seed FAA sites"),
        (BACKFILL, "Backfill 90 days"),
        (REBUILD, "Rebuild derived tables"),
        (REFRESH, "Refresh after review"),
    ]
    QUEUED, RUNNING, OK, FAILED = "queued", "running", "ok", "failed"

    kind = models.CharField(max_length=20, choices=KINDS)
    status = models.CharField(max_length=10, default=QUEUED, db_index=True)
    log = models.TextField(blank=True)
    upload = models.BinaryField(null=True, blank=True)  # optional file for seed_sites fallback
    requested_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]


class WorkerState(models.Model):
    """Single row: heartbeat and the last nightly run, so the Operations page can show health."""

    heartbeat = models.DateTimeField(null=True, blank=True)
    last_nightly = models.DateField(null=True, blank=True)
    current = models.CharField(max_length=200, blank=True)
    # The counting-rules version the last full rebuild applied (tracker.rebuild_flag), and the overnight slot.
    rules_version = models.PositiveIntegerField(default=0)
    last_overnight = models.DateField(null=True, blank=True)  # local date the overnight check last ran
    last_full_rebuild = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class AutoConfirmRule(models.Model):
    """Admin-defined rule: discovered aircraft whose FAA registrant matches are confirmed automatically.

    A rule is an admin decision, so applying it is not "automation overwriting a human": it only
    ever moves aircraft out of `discovered`, never touches confirmed or rejected ones, and records
    each confirmation in the admin log.
    """

    registrant_contains = models.CharField(
        max_length=200, help_text="Case-insensitive text to look for in the FAA registrant name.")
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL,
                                 help_text="Optional: also set this operator when it is not set yet.")
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                help_text="Optional: also set this program when it is not set yet.")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["registrant_contains"]

    def __str__(self):
        return f"Registrant contains “{self.registrant_contains}”"


class PlaceSuggestion(models.Model):
    """Cached OpenStreetMap lookup of named hospitals/helipads near a landing-cluster location.

    Keyed by coordinates rounded to 3 decimals (~100 m) so rebuilding clusters reuses earlier
    answers instead of querying again. Names are suggestions only; an admin creates the Site.
    """

    lat3 = models.DecimalField(max_digits=7, decimal_places=3)
    lon3 = models.DecimalField(max_digits=8, decimal_places=3)
    name = models.CharField(max_length=200, blank=True)  # best match; blank = nothing named nearby
    kind = models.CharField(max_length=20, blank=True)   # "hospital" or "helipad"
    distance_m = models.PositiveIntegerField(null=True, blank=True)
    osm_url = models.URLField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=2, blank=True)
    candidates = models.JSONField(default=list)          # up to 5: [{name, kind, distance_m, osm_url, city, state}]
    error = models.CharField(max_length=200, blank=True)  # set when the lookup failed (retried later)
    looked_up_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("lat3", "lon3")]
