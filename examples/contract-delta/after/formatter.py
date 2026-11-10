def format_item(value, *, strict):
    if strict and value is None:
        raise ValueError("value is required")
    return str(value)
