from src.resource_loader import read_resource

def test_read_resource_missing():
    assert read_resource("nonexistent_file_path.txt") is None
