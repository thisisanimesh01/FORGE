def process_data(raw_items: list) -> list:
    """Safe data processing function."""
    return [item.strip() for item in raw_items if isinstance(item, str)]
