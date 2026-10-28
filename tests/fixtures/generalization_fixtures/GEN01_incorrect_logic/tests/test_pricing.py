import unittest
from src.pricing import calculate_discount

class TestPricing(unittest.TestCase):
    def test_normal_discount(self):
        self.assertAlmostEqual(calculate_discount(100.0, 20.0), 80.0)

    def test_zero_discount(self):
        self.assertAlmostEqual(calculate_discount(50.0, 0.0), 50.0)

if __name__ == "__main__":
    unittest.main()
