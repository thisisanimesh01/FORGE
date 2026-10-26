from pathlib import Path

def resolve_user_path(base_dir: str, user_path: str) -> Path:
    return Path(base_dir) / user_path
