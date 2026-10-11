"""Operator acquisitions: an operator bought by another becomes one of the buyer's programs."""

from django.db import transaction

from .models import Aircraft, AutoConfirmRule, Mission, MonthlyAggregate, Program, ServicePeriod


@transaction.atomic
def absorb_operator(acquired, buyer):
    """Fold `acquired` into `buyer` and delete it. Returns (program, aircraft moved).

    - A program named like the acquired operator (reused if one exists, ignoring case) now belongs to the buyer.
    - The acquired operator's aircraft move to the buyer; those without a program join that program.
    - Its other programs and monthly aggregates move to the buyer, and every aircraft in any of those programs
      (or the new one) is the buyer's.
    - Its auto-confirm rules are kept, now for the buyer, and set the new program (unless they name one already).
    """
    if acquired.pk == buyer.pk:
        raise ValueError("An operator cannot acquire itself.")
    program = Program.objects.filter(name__iexact=acquired.name).first() or Program(name=acquired.name)
    program.operator = buyer
    if acquired.notes and not program.notes:
        program.notes = acquired.notes
    program.save()
    aircraft = Aircraft.objects.filter(operator=acquired)
    moved = aircraft.count()
    aircraft.filter(program__isnull=True).update(program=program)
    aircraft.update(operator=buyer)
    moved_programs = [program.pk] + list(Program.objects.filter(operator=acquired).values_list("pk", flat=True))
    Program.objects.filter(pk__in=moved_programs).update(operator=buyer)
    Aircraft.objects.filter(program__in=moved_programs).exclude(operator=buyer).update(operator=buyer)
    rules = AutoConfirmRule.objects.filter(operator=acquired)
    rules.filter(program__isnull=True).update(program=program)
    rules.update(operator=buyer)
    MonthlyAggregate.objects.filter(operator=acquired).update(operator=buyer)  # until the queued refresh
    Mission.objects.filter(operator=acquired).update(operator=buyer)
    ServicePeriod.objects.filter(operator=acquired).update(operator=buyer)  # past periods follow the buyer too
    acquired.delete()
    return program, moved
