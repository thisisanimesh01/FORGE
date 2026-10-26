from src.math_clean import safe_divide

def test_safe_divide():
    assert safe_divide(10.0, 2.0) == 5.0
    assert safe_divide(10.0, 0.0, default=-1.0) == -1.0
