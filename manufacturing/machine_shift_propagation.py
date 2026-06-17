from django.db.models import Q

from manufacturing.models import Machine


def machine_department_shift_keys(machine):
    keys = []
    for value in (getattr(machine, "category", None), getattr(machine, "type", None)):
        label = str(value or "").strip()
        if label and label.lower() not in {item.lower() for item in keys}:
            keys.append(label)
    return keys


def propagate_machine_department_shift_configuration(machine, extra_keys=None):
    keys = machine_department_shift_keys(machine)
    for value in extra_keys or []:
        label = str(value or "").strip()
        if label and label.lower() not in {item.lower() for item in keys}:
            keys.append(label)
    if not keys:
        return 0

    query = Q()
    for key in keys:
        query |= Q(category__iexact=key) | Q(type__iexact=key)

    return (
        Machine.objects.filter(company=machine.company)
        .filter(query)
        .exclude(pk=machine.pk)
        .update(
            use_factory_shifts=machine.use_factory_shifts,
            shift_configuration=machine.shift_configuration or {},
        )
    )
