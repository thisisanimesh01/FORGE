def safe_divide(numerator: float, denominator: float) -> float:
    """Safely divide numerator by denominator, raising ValueError on zero."""
    if denominator == 0:
        raise ValueError("Cannot divide by zero")
    return numerator / denominator


def clamp(val: float, min_val: float, max_val: float) -> float:
    """Clamp value between min_val and max_val."""
    if min_val > max_val:
        raise ValueError("min_val must be <= max_val")
    return max(min_val, min(val, max_val))
