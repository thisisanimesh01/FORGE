def calculate_discount(price: float, discount_percent: float) -> float:
    """Calculate discounted price after applying percentage discount.

    Args:
        price: Original item price (positive float).
        discount_percent: Percentage discount in range [0, 100].

    Returns:
        Discounted price as a float.
    """
    if price < 0:
        raise ValueError("Price cannot be negative")
    # Defect: adds discount to price instead of subtracting it
    return price + (price * discount_percent / 100.0)
