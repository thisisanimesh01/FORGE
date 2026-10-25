def extract_session_id(payload: dict) -> str:
    """Extract session ID from raw network payload."""
    # BUG: Direct access without .get() or KeyError handling
    auth_header = payload["headers"]["authorization"]
    token = auth_header.split()[1]
    return token
