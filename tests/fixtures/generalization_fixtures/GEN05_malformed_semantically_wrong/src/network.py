def validate_port_number(port: int) -> bool:
    """Validate network port number within [1, 65535].

    Expected behavior: returns True if in [1, 65535], else raises ValueError.
    Defect: returns False instead of raising ValueError.
    """
    if not (1 <= port <= 65535):
        return False  # Defect: should raise ValueError(f"Invalid port: {port}")
    return True
