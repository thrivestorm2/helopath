"""No aircraft is confirmed without an operator: confirmed aircraft with none take their program's operator, and
the rest go back to Discovered (the review queue). Each change is logged in the admin history."""

from django.db import migrations

CONFIRMED, DISCOVERED = "confirmed", "discovered"


def forwards(apps, schema_editor):
    Aircraft = apps.get_model("tracker", "Aircraft")
    LogEntry = apps.get_model("admin", "LogEntry")
    ContentType = apps.get_model("contenttypes", "ContentType")
    User = apps.get_model("auth", "User")
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    content_type = ContentType.objects.get_or_create(app_label="tracker", model="aircraft")[0]
    filled = unconfirmed = 0
    for a in Aircraft.objects.filter(status=CONFIRMED, operator__isnull=True).select_related("program"):
        if a.program and a.program.operator_id:
            a.operator_id = a.program.operator_id
            a.save(update_fields=["operator"])
            message, filled = f"Operator set to the operator of {a.program.name} (confirmed aircraft need one)", filled + 1
        else:
            a.status = DISCOVERED
            a.save(update_fields=["status"])
            message, unconfirmed = "Unconfirmed: no operator (confirmed aircraft need one)", unconfirmed + 1
        if owner:
            LogEntry.objects.create(user_id=owner.pk, content_type_id=content_type.pk, object_id=str(a.pk),
                                    object_repr=a.tail_number[:200], action_flag=2, change_message=message)
    ServicePeriod = apps.get_model("tracker", "ServicePeriod")
    gaps = ServicePeriod.objects.filter(operator__isnull=True, aircraft__status=CONFIRMED).exists()
    if filled or unconfirmed or gaps:  # confirmed-only figures change: recount them shortly after deploy
        apps.get_model("tracker", "PendingRebuild").objects.create(
            kind="recount", reason="confirmed aircraft and periods now need an operator")
    if filled or unconfirmed:
        print(f"\n  Confirmed aircraft without an operator: {filled} took their program's operator, "
              f"{unconfirmed} moved back to the review queue.")


class Migration(migrations.Migration):
    dependencies = [
        ("tracker", "0030_site_origin_csv_import"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
