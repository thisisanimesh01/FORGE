import os
from pathlib import Path

def get_file_path(base_dir: str, requested_file: str) -> str:
    """Retrieve file path within base_dir."""
    # BUG: Vulnerable to path traversal (e.g. '../../etc/passwd')
    full_path = os.path.join(base_dir, requested_file)
    return os.path.abspath(full_path)
