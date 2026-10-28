def check_temperature_threshold(celsius: float) -> str:
    """Evaluate temperature reading against physical and operational bounds.

    Expected behavior: raises ValueError when celsius is below absolute zero (-273.15).
    Defect: returns 'CLIPPED_LOW' instead of raising ValueError.
    """
    if celsius < -273.15:
        return "CLIPPED_LOW"  # Defect: should raise ValueError("Temperature below absolute zero")
    if celsius > 100.0:
        return "HIGH"
    return "NORMAL"
