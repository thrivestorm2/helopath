"""Operator acquisitions: an operator bought by another becomes one of the buyer's programs."""

from django.db import transaction

from .models import Aircraft, AutoConfirmRule, MonthlyAggregate, Program


@transaction.atomic
def absorb_operator(acquired, buyer):
    """Fold `acquired` into `buyer` and delete it. Returns (program, aircraft moved).

    - A program named like the acquired operator (reused if one exists, ignoring case) now belongs to the buyer.
    - The acquired operator's aircraft move to the buyer; those without a program join that program.
    - Its other programs, auto-confirm rules and monthly aggregates move to the buyer.
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
    Program.objects.filter(operator=acquired).update(operator=buyer)
    AutoConfirmRule.objects.filter(operator=acquired).update(operator=buyer)
    MonthlyAggregate.objects.filter(operator=acquired).update(operator=buyer)  # until the queued refresh
    acquired.delete()
    return program, moved
