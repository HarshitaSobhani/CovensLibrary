from django.db import IntegrityError, transaction

from .models import IdSequence


def next_value(name, year=0):
    """Return the next integer for (name, year). Must be called inside the creating transaction.

    The row is locked until that transaction ends, so concurrent callers never get duplicates.
    A rolled-back transaction leaves a gap-free counter (the increment rolls back too).
    """
    with transaction.atomic():
        try:
            with transaction.atomic():
                IdSequence.objects.get_or_create(name=name, year=year)
        except IntegrityError:  # pragma: no cover - concurrent first creation
            pass
        seq = IdSequence.objects.select_for_update().get(name=name, year=year)
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
        return seq.last_value
