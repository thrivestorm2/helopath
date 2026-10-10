"""Auto-confirm rules (admin-defined). See AutoConfirmRule."""
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User

from .models import Aircraft, AutoConfirmRule


def matching(rule, statuses=(Aircraft.DISCOVERED,)):
    return Aircraft.objects.filter(status__in=statuses, registrant_hint__icontains=rule.registrant_contains.strip())


def apply_auto_confirm_rules():
    """Confirm discovered aircraft that match an active rule, and give matching aircraft the rule's
    operator and program when they have none. Never touches rejected aircraft, or an operator or program
    already set.
    Returns how many aircraft changed."""
    changed = 0
    # Rules seeded without an author are logged under the first superuser (the owner).
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    for rule in AutoConfirmRule.objects.filter(is_active=True).select_related("operator", "program", "created_by"):
        if not rule.registrant_contains.strip():
            continue
        user = rule.created_by or owner
        for aircraft in matching(rule, (Aircraft.DISCOVERED, Aircraft.CONFIRMED)):
            fields, notes = [], []
            if aircraft.status == Aircraft.DISCOVERED:
                aircraft.status = Aircraft.CONFIRMED
                fields.append("status")
                notes.append("auto-confirmed")
            if rule.operator_id and not aircraft.operator_id:
                aircraft.operator = rule.operator
                fields.append("operator")
                notes.append(f"operator set to {rule.operator}")
            if rule.program_id and not aircraft.program_id:
                aircraft.program = rule.program
                fields.append("program")
                notes.append(f"program set to {rule.program}")
            if not fields:
                continue
            aircraft.save(update_fields=fields)
            if user:
                LogEntry.objects.log_actions(
                    user_id=user.pk, queryset=[aircraft], action_flag=CHANGE, single_object=True,
                    change_message=f"{', '.join(notes).capitalize()} by rule: registrant contains "
                                   f"'{rule.registrant_contains}'")
            changed += 1
    return changed
