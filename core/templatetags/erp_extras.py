from django import template

register = template.Library()


@register.filter
def with_labels(row, headers):
    """Pair each cell with its column heading so tables can collapse into labelled cards on phones."""
    return list(zip(headers, row))


@register.filter
def get_item(mapping, key):
    """dict lookup by a variable key: {{ d|get_item:k }} (tries the key as given, then as str / int)."""
    if not mapping:
        return ""
    raw = getattr(key, "value", key)          # ModelChoiceIteratorValue (checkbox widgets) -> its pk
    for k in (raw, str(raw)):
        try:
            if k in mapping:
                return mapping[k]
        except TypeError:                     # unhashable key
            continue
    try:
        return mapping.get(int(str(raw)), "")
    except (TypeError, ValueError):
        return ""
