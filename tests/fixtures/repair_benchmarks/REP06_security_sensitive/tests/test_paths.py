import pytest
from pathlib import Path
from src.path_utils import resolve_user_path

def test_resolve_user_path(tmp_path: Path):
    safe_base = tmp_path / "sandbox"
    safe_base.mkdir()
    res = resolve_user_path(str(safe_base), "doc.txt")
    assert res == safe_base / "doc.txt"

    with pytest.raises(PermissionError, match="Path traversal detected"):
        resolve_user_path(str(safe_base), "../../etc/passwd")
