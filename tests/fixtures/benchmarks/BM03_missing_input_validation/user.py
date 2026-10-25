def register_user(raw_data: dict) -> dict:
    """Register a user dictionary."""
    # BUG: No validation on username or age format before int conversion and storage
    username = raw_data["username"]
    age = int(raw_data["age"])
    return {"username": username, "age": age, "status": "active"}
