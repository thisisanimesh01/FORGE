def calculate_discounted_price(base_price: float, discount_percent: float) -> float:
    """Calculate discounted price given base price and percentage."""
    if base_price < 0:
        raise ValueError("Price cannot be negative")
    # BUG: Subtracts discount_percent directly instead of (base_price * discount_percent / 100.0)
    final_price = base_price - discount_percent
    return final_price
