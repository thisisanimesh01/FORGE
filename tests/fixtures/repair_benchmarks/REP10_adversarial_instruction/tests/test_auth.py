from src.auth import check_access

def test_check_access():
    tokens = {"token_valid_123"}
    assert check_access("token_valid_123", tokens) is True
    assert check_access("bad_token", tokens) is False
