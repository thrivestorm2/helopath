from urllib.parse import urlencode

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.html import format_html, format_html_join
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from . import periods
from .models import Aircraft, AutoConfirmRule, Operator, Program, ServicePeriod, Site
from .ops import queue_refresh
from .faa import merge_duplicate_faa_sites
from .places import clear_covered_clusters
from .rebuild_flag import flag_rebuild

admin.site.site_header = "Air Medical Tracker admin"
admin.site.site_title = "Air Medical Tracker"
admin.site.disable_action("delete_selected")  # deactivate instead of delete


class ReturnToPageMixin:
    """Edit links on the app's pages add ?next=<page>; Save then returns there instead of the admin list.
    The admin form posts back to its own URL, so `next` is still in the query string on save."""

    def _next_url(self, request):
        nxt = request.GET.get("next", "")
        ok = nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure())
        return nxt if ok else None

    def response_add(self, request, obj, post_url_continue=None):
        response = super().response_add(request, obj, post_url_continue)
        nxt = self._next_url(request)
        return HttpResponseRedirect(nxt) if nxt and "_save" in request.POST else response

    def response_change(self, request, obj):
        response = super().response_change(request, obj)
        nxt = self._next_url(request)
        if nxt and "_save" in request.POST:
            return HttpResponseRedirect(nxt)
        if nxt and "_continue" in request.POST and isinstance(response, HttpResponseRedirect):
            sep = "&" if "?" in response["Location"] else "?"
            response["Location"] += sep + urlencode({"next": nxt})  # keep it for the next save
        return response


class SiteResource(resources.ModelResource):
    class Meta:
        model = Site
        fields = ("id", "name", "site_type", "faa_id", "city", "state", "lat", "lon", "elevation_ft",
                  "radius_m", "origin", "reviewed", "is_active", "notes")


class AircraftResource(resources.ModelResource):
    operator = fields.Field(attribute="operator", column_name="operator",
                            widget=ForeignKeyWidget(Operator, field="name"))
    program = fields.Field(attribute="program", column_name="program", widget=ForeignKeyWidget(Program, field="name"))
    base = fields.Field(attribute="base", column_name="base_site_id", widget=ForeignKeyWidget(Site))

    class Meta:
        model = Aircraft
        import_id_fields = ("tail_number",)
        fields = ("tail_number", "call_sign", "icao_hex", "status", "origin", "operator", "program", "base",
                  "active_from", "active_to", "notes")

    def before_save_instance(self, instance, row, **kwargs):
        if instance.pk is None:  # a CSV-added aircraft is a manual, confirmed one
            instance.origin = "manual"
            instance.status = instance.status or Aircraft.CONFIRMED
        instance.icao_hex = (instance.icao_hex or "").lower()


@admin.action(description="Deactivate selected sites")
def deactivate(modeladmin, request, queryset):
    n = queryset.update(is_active=False)
    for obj in queryset:
        modeladmin.log_change(request, obj, "Deactivated")
    if n:
        flag_rebuild(f"{n} site{'s' if n != 1 else ''} deactivated")
    modeladmin.message_user(request, f"Deactivated {n} site(s). Takes effect after the next rebuild.")


@admin.action(description="Mark selected sites reviewed")
def mark_reviewed(modeladmin, request, queryset):
    queryset.update(reviewed=True)
    for obj in queryset:
        modeladmin.log_change(request, obj, "Marked reviewed")


@admin.register(Site)
class SiteAdmin(ReturnToPageMixin, ImportExportModelAdmin):
    resource_classes = [SiteResource]
    list_display = ("name", "site_type", "faa_id", "city", "state", "radius_m", "origin", "reviewed", "is_active")
    list_filter = ("site_type", "state", "reviewed", "is_active", "origin")
    search_fields = ("name", "faa_id", "city")
    actions = [mark_reviewed, deactivate]
    autocomplete_fields = ("weather_hangar",)
    change_form_template = "admin/tracker/site/change_form.html"
    fieldsets = (
        (None, {"fields": ("name", "site_type", "faa_id", "city", "state")}),
        ("Location (click the map to set)", {"fields": ("lat", "lon", "elevation_ft", "radius_m")}),
        ("Bad weather", {"fields": ("weather_hangar",)}),
        ("Status", {"fields": ("origin", "reviewed", "is_active", "notes")}),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        flag_rebuild(f"site {obj.name} {'edited' if change else 'added'}")
        for keep, names in merge_duplicate_faa_sites():
            messages.success(request, f"Merged {', '.join(names)} into {keep.name}: same FAA ID {keep.faa_id}.")
        cleared = clear_covered_clusters(obj) if Site.objects.filter(pk=obj.pk).exists() else 0
        if cleared:
            messages.success(request, f"{obj.name} replaces {cleared} landing spot{'s' if cleared != 1 else ''} "
                                      "in the Cluster queue.")
        messages.warning(request, "Site changes take effect after the next rebuild (see the banner at the top).")


@admin.register(Operator)
class OperatorAdmin(ReturnToPageMixin, ImportExportModelAdmin):
    list_display = ("name", "parent_company")
    search_fields = ("name", "parent_company")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Program)
class ProgramAdmin(ReturnToPageMixin, ImportExportModelAdmin):
    """Programs can be deleted: their aircraft are kept and simply have no program (Aircraft.program is SET_NULL).
    A program that still has aircraft can be deleted only by a superuser; Admin-group users can delete empty ones."""

    list_display = ("name", "operator", "aircraft_count")
    list_filter = ("operator",)
    search_fields = ("name", "operator__name")
    autocomplete_fields = ("operator",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_aircraft=Count("aircraft"))

    @admin.display(description="Aircraft", ordering="n_aircraft")
    def aircraft_count(self, obj):
        return obj.n_aircraft

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.operators_filled:
            queue_refresh(request.user)  # monthly totals are kept per operator
            self.message_user(request, f"{obj.operators_filled} aircraft in {obj} had no operator; set to {obj.operator}.")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not request.user.is_superuser and obj.aircraft.exists():
            return False
        return super().has_delete_permission(request, obj)


class ServicePeriodInline(admin.TabularInline):
    """Past service periods (closed with 'Active until'); editable to correct dates or details."""

    model = ServicePeriod
    extra = 0
    fields = ("start", "end", "base", "call_sign", "operator", "program", "notes")
    autocomplete_fields = ("base", "operator", "program")
    verbose_name = "past service period"
    verbose_name_plural = "Service history (past periods)"
    ordering = ("-end",)


class AircraftForm(forms.ModelForm):
    class Meta:
        model = Aircraft
        fields = "__all__"

    def clean(self):
        data = super().clean()
        start, end = data.get("active_from"), data.get("active_to")
        if start and end and end < start:
            raise forms.ValidationError("'Active until' is before 'Active from'.")
        if data.get("retired") and not end and not (self.instance.pk and self.instance.retired):
            data["active_to"] = timezone.localdate()  # retiring today unless a date is given
        return data


@admin.register(Aircraft)
class AircraftAdmin(ReturnToPageMixin, ImportExportModelAdmin):
    resource_classes = [AircraftResource]
    form = AircraftForm
    inlines = [ServicePeriodInline]
    list_display = ("tail_number", "call_sign", "icao_hex", "status", "origin", "operator", "base", "inferred_base",
                    "qualifying_visits", "last_seen")
    list_filter = ("status", "retired", "origin", "operator", "program")
    search_fields = ("tail_number", "call_sign", "icao_hex", "program__name")
    autocomplete_fields = ("operator", "program", "base")
    readonly_fields = ("inferred_base", "inferred_base_lat", "inferred_base_lon", "make_model", "registrant_hint",
                       "callsign_hint", "operator_hint", "top_hospitals", "qualifying_visits",
                       "distinct_hospitals", "first_seen", "last_seen", "discovered_at", "discovery_source",
                       "discovery_rule")
    fieldsets = (
        (None, {"fields": ("tail_number", "icao_hex", "status", "origin")}),
        ("Admin-owned", {"fields": ("call_sign", "call_sign_reviewed", "operator", "program", "program_reviewed", "base", "notes")}),
        ("Social pages (Look it up: blank uses the program's, then the operator's, else a search)",
         {"fields": ("facebook_url", "instagram_url")}),
        ("Current service period", {"fields": ("active_from", "active_to", "retired")}),
        ("Automation-owned (read-only)", {"fields": readonly_fields}),
    )

    @admin.display(description="Top hospitals")
    def top_hospitals(self, obj):
        """One hospital per line, e.g. "OSF Saint Francis (5)"."""
        return format_html_join(format_html("<br>"), "{}", ((h,) for h in obj.top_hospitals_list)) or "—"

    def get_readonly_fields(self, request, obj=None):
        # Auto-discovered identity comes from the registry; manual adds may set it.
        extra = ("tail_number", "icao_hex", "origin") if obj and obj.origin == "auto" else ()
        return self.readonly_fields + extra

    def save_model(self, request, obj, form, change):
        if not change:
            obj.origin, obj.icao_hex = "manual", obj.icao_hex.lower()
            if obj.status == Aircraft.DISCOVERED:
                obj.status = Aircraft.CONFIRMED
        had_operator = obj.operator_id
        was_retired = change and form.initial.get("retired", False)
        super().save_model(request, obj, form, change)
        if obj.operator_id != had_operator:
            queue_refresh(request.user)
            self.message_user(request, f"Operator set to {obj.operator}, the operator of {obj.program}.")
        self._service_period(request, obj, form, was_retired)

    def _service_period(self, request, obj, form, was_retired):
        """'Active until', 'Retired': close the period, end service, or start again."""
        where = (obj.base or obj.inferred_base).name if (obj.base or obj.inferred_base) else "no base"
        if obj.retired and not was_retired:
            periods.retire(obj, obj.active_to)
            msg = f"{obj} retired: service ended {obj.active_to}; later flights are not counted."
        elif was_retired and not obj.retired:
            ended = obj.active_to
            periods.unretire(obj)
            msg = (f"{obj} back in service: new period from {obj.active_from} with no base or call sign."
                   if ended else f"{obj} back in service.")
        elif obj.active_to and not obj.retired:
            end = obj.active_to
            periods.close_period(obj, end)
            msg = (f"Service at {where} closed on {end}; new period from {obj.active_from} with the same operator "
                   f"and program, no base or call sign.")
        else:
            return
        flag_rebuild(f"service period changed for {obj.tail_number}")
        self.message_user(request, msg)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    """Read-only audit trail: who changed what, when."""

    list_display = ("action_time", "user", "content_type", "object_repr", "action_flag", "change_message")
    list_filter = ("action_flag", "content_type", "user")
    search_fields = ("object_repr", "change_message")
    date_hierarchy = "action_time"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AutoConfirmRule)
class AutoConfirmRuleAdmin(admin.ModelAdmin):
    list_display = ("registrant_contains", "operator", "program", "is_active", "created_by", "created_at")
    autocomplete_fields = ("operator", "program")
    list_filter = ("is_active",)
    readonly_fields = ("created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
