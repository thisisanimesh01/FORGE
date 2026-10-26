from src.legacy_api import get_user_summary

def test_legacy_caller_tuple_unpacking():
    # Legacy callers expect a 2-tuple that can be unpacked
    name, role = get_user_summary(1)
    assert name == "alice"
    assert role == "admin"
