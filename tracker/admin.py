from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from .models import Aircraft, AutoConfirmRule, Operator, Program, Site

admin.site.site_header = "Air Medical Tracker admin"
admin.site.site_title = "Air Medical Tracker"
admin.site.disable_action("delete_selected")  # deactivate instead of delete


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
        fields = ("tail_number", "icao_hex", "status", "origin", "operator", "program", "base",
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
    modeladmin.message_user(request, f"Deactivated {n} site(s). Takes effect after the next rebuild.")


@admin.action(description="Mark selected sites reviewed")
def mark_reviewed(modeladmin, request, queryset):
    queryset.update(reviewed=True)
    for obj in queryset:
        modeladmin.log_change(request, obj, "Marked reviewed")


@admin.register(Site)
class SiteAdmin(ImportExportModelAdmin):
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
        messages.warning(
            request,
            format_html('Site changes take effect after the next rebuild. <a class="button" href="{}">Queue rebuild</a>',
                        reverse("queue_rebuild")),
        )


@admin.register(Operator)
class OperatorAdmin(ImportExportModelAdmin):
    list_display = ("name", "parent_company")
    search_fields = ("name", "parent_company")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Program)
class ProgramAdmin(ImportExportModelAdmin):
    list_display = ("name", "operator", "aircraft_count")
    list_filter = ("operator",)
    search_fields = ("name", "operator__name")
    autocomplete_fields = ("operator",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_aircraft=Count("aircraft"))

    @admin.display(description="Aircraft", ordering="n_aircraft")
    def aircraft_count(self, obj):
        return obj.n_aircraft

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Aircraft)
class AircraftAdmin(ImportExportModelAdmin):
    resource_classes = [AircraftResource]
    list_display = ("tail_number", "icao_hex", "status", "origin", "operator", "base", "inferred_base",
                    "qualifying_visits", "last_seen")
    list_filter = ("status", "origin", "operator", "program")
    search_fields = ("tail_number", "icao_hex", "program__name")
    autocomplete_fields = ("operator", "program", "base")
    readonly_fields = ("inferred_base", "inferred_base_lat", "inferred_base_lon", "make_model", "registrant_hint",
                       "callsign_hint", "operator_hint", "top_hospitals_hint", "qualifying_visits",
                       "distinct_hospitals", "first_seen", "last_seen", "discovered_at", "discovery_source",
                       "discovery_rule")
    fieldsets = (
        (None, {"fields": ("tail_number", "icao_hex", "status", "origin")}),
        ("Admin-owned", {"fields": ("operator", "program", "base", "active_from", "active_to", "notes")}),
        ("Automation-owned (read-only)", {"fields": readonly_fields}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Auto-discovered identity comes from the registry; manual adds may set it.
        extra = ("tail_number", "icao_hex", "origin") if obj and obj.origin == "auto" else ()
        return self.readonly_fields + extra

    def save_model(self, request, obj, form, change):
        if not change:
            obj.origin, obj.icao_hex = "manual", obj.icao_hex.lower()
            if obj.status == Aircraft.DISCOVERED:
                obj.status = Aircraft.CONFIRMED
        super().save_model(request, obj, form, change)

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
    list_display = ("registrant_contains", "operator", "is_active", "created_by", "created_at")
    list_filter = ("is_active",)
    readonly_fields = ("created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
