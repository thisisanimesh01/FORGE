from src.pricing import calculate_discounted_price

def test_calculate_discounted_price():
    assert calculate_discounted_price(100.0, 20.0) == 80.0
    assert calculate_discounted_price(50.0, 50.0) == 0.0
