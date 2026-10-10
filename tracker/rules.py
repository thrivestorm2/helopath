"""Auto-confirm rules (admin-defined). See AutoConfirmRule."""
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User

from .models import Aircraft, AutoConfirmRule


def matching(rule):
    return Aircraft.objects.filter(status=Aircraft.DISCOVERED,
                                   registrant_hint__icontains=rule.registrant_contains.strip())


def apply_auto_confirm_rules():
    """Confirm discovered aircraft that match an active rule. Returns how many changed."""
    changed = 0
    # Rules seeded without an author are logged under the first superuser (the owner).
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    for rule in AutoConfirmRule.objects.filter(is_active=True).select_related("operator", "created_by"):
        if not rule.registrant_contains.strip():
            continue
        user = rule.created_by or owner
        for aircraft in matching(rule):
            aircraft.status = Aircraft.CONFIRMED
            fields = ["status"]
            if rule.operator_id and not aircraft.operator_id:
                aircraft.operator = rule.operator
                fields.append("operator")
            aircraft.save(update_fields=fields)
            if user:
                LogEntry.objects.log_actions(
                    user_id=user.pk, queryset=[aircraft], action_flag=CHANGE, single_object=True,
                    change_message=f"Auto-confirmed by rule: registrant contains '{rule.registrant_contains}'")
            changed += 1
    return changed
