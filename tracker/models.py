"""Data model (spec section 9).

Ownership rule: fields prefixed `inferred_` plus the evidence and hint fields on
Aircraft are written by automation; `operator`, `base`, `status` and `notes` belong
to admins and are never written by discovery or rebuild.
"""
from django.db import models

NO_PATIENT_INFO = "Do not record patient information of any kind here."

SOURCE_CHOICES = [("adsblol", "adsb.lol"), ("adsbx", "ADS-B Exchange sample")]


class Operator(models.Model):
    name = models.CharField(max_length=200, unique=True)
    parent_company = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Program(models.Model):
    """A flight program: the service name on the aircraft (e.g. a hospital-branded program), flown by an operator."""

    name = models.CharField(max_length=200, unique=True)
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL, related_name="programs",
                                 help_text="The company that flies it, if known.")
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Site(models.Model):
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
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["state", "city", "name"]

    def __str__(self):
        return f"{self.name} ({self.faa_id})" if self.faa_id else self.name


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


class Aircraft(models.Model):
    DISCOVERED, CONFIRMED, REJECTED = "discovered", "confirmed", "rejected"
    STATUS_CHOICES = [(DISCOVERED, "Discovered"), (CONFIRMED, "Confirmed"), (REJECTED, "Rejected")]
    ORIGIN_CHOICES = [("auto", "Auto-discovered"), ("manual", "Manual")]
    VISIBLE = (DISCOVERED, CONFIRMED)

    tail_number = models.CharField(max_length=10, unique=True)
    icao_hex = models.CharField(max_length=6, unique=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DISCOVERED, db_index=True)
    origin = models.CharField(max_length=10, choices=ORIGIN_CHOICES, default="auto")

    # Admin-owned.
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL)
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL, related_name="aircraft")
    base = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="based_aircraft")
    active_from = models.DateField(null=True, blank=True)
    active_to = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text=NO_PATIENT_INFO)

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
        return self.tail_number

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
