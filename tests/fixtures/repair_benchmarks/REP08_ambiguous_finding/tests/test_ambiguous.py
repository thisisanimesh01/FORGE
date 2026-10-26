from src.ambiguous import format_output

def test_format_output():
    assert format_output("  test  ") == "test"
