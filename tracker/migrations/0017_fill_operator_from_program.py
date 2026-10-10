"""Fill in operators already known from programs.

1. A program with no operator whose aircraft that have one all share the same operator gets that operator.
2. An aircraft with a program but no operator gets the program's operator.
Never replaces an operator already set. Queues a refresh so the monthly totals (kept per operator) catch up."""

from django.db import migrations


def fill(apps, schema_editor):
    Aircraft = apps.get_model("tracker", "Aircraft")
    Program = apps.get_model("tracker", "Program")
    OpsTask = apps.get_model("tracker", "OpsTask")
    for program in Program.objects.filter(operator__isnull=True):
        operators = set(Aircraft.objects.filter(program=program, operator__isnull=False)
                        .values_list("operator_id", flat=True))
        if len(operators) == 1:
            program.operator_id = operators.pop()
            program.save(update_fields=["operator"])
    changed = 0
    for program in Program.objects.filter(operator__isnull=False):
        changed += Aircraft.objects.filter(program=program, operator__isnull=True).update(operator_id=program.operator_id)
    if changed and not OpsTask.objects.filter(kind="refresh", status="queued").exists():
        OpsTask.objects.create(kind="refresh")


class Migration(migrations.Migration):
    dependencies = [("tracker", "0016_rule_program")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
