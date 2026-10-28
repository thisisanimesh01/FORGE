import unittest
from src.sensor import check_temperature_threshold

class TestSensor(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(check_temperature_threshold(25.0), "NORMAL")

    def test_sub_absolute_zero_raises(self):
        """Demonstrates defect: temperatures below -273.15 must raise ValueError."""
        with self.assertRaises(ValueError):
            check_temperature_threshold(-300.0)

if __name__ == "__main__":
    unittest.main()
